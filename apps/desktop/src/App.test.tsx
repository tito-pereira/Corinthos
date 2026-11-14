// SPDX-License-Identifier: MPL-2.0
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { AgentSession, BackendSnapshot, ContextProjection, FlowView, ImportDiscovery, ProjectEntry, ProjectSummary, SessionHandoff, SwitchView, WorkItem, WorkScope } from "./bridge";

const { invoke, listen } = vi.hoisted(() => ({ invoke: vi.fn(), listen: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen }));

const methods = ["projects.list", "project.summary", "project.open", "work.tree", "work.get", "work.create", "work.status"];
const ready: Extract<BackendSnapshot, { connection: "ready" }> = {
  connection: "ready",
  handshake: { protocol_version: 2, version: "0.1.0", methods },
  providers: { providers: [{
    provider: "codex", supported: true, configuration: "default", executable: null,
    observation: "NOT_OBSERVED", capabilities: null,
  }] },
};

const legacy: ProjectEntry = {
  root: "C:\\project", display_name: "Project", last_accessed_at: "2026-01-01T00:00:00Z",
  availability: "AVAILABLE", initialized: true, storage: "LEGACY", schema_version: 6,
};
const missing: ProjectEntry = {
  ...legacy, root: "C:\\missing", display_name: "Missing project", availability: "MISSING",
  initialized: false, storage: "ABSENT", schema_version: null,
};
const summary: ProjectSummary = {
  root: legacy.root, display_name: legacy.display_name, exists: true, is_directory: true,
  initialized: true, storage: "CURRENT", schema_version: 7,
  active_project: { contract_id: "project_contract", revision_id: "project_rev" },
  active_workflow: { contract_id: "workflow_contract", revision_id: "workflow_rev" },
  work_items: { total: 0, by_scope: {}, by_status: {} },
};
const item: WorkItem = {
  work_item_id: "work_1", parent_work_item_id: null, objective: "Handle nested work",
  scope_level: "CHANGE", status: "OPEN", updated_at: "2026-01-02T00:00:00Z",
  allowed_transitions: ["ACTIVE", "BLOCKED", "CANCELLED"],
};

const env = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean };
let host: HTMLDivElement;
let root: Root;
let lifecycle: (event: { payload: { method: string } }) => void;
let release: ReturnType<typeof vi.fn>;

beforeEach(() => {
  env.IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  release = vi.fn();
  listen.mockImplementation(async (_name, callback) => {
    lifecycle = callback;
    return release;
  });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  env.IS_REACT_ACT_ENVIRONMENT = false;
});

async function render() {
  await act(async () => root.render(<App />));
}

async function click(label: string) {
  const target = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === label);
  if (!target) throw new Error(`Button missing: ${label}`);
  await act(async () => target.click());
}

async function field(id: string, value: string) {
  const target = host.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(id);
  if (!target) throw new Error(`Input missing: ${id}`);
  const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(target), "value")?.set;
  await act(async () => {
    setter?.call(target, value);
    target.dispatchEvent(new Event(target instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}

describe("desktop Home and Projects", () => {
  it("loads known and missing projects passively once the backend negotiates v2", async () => {
    let respond: (value: BackendSnapshot) => void = () => {};
    invoke.mockReturnValueOnce(new Promise<BackendSnapshot>((resolve) => { respond = resolve; }));
    invoke.mockResolvedValueOnce({ projects: [legacy, missing], next_offset: null });
    await render();
    expect(host.textContent).toContain("Connecting");
    await act(async () => respond(ready));
    expect(host.textContent).toContain("0.1.0");
    expect(host.textContent).toContain("v2");
    expect(host.textContent).toContain("Not yet observed");
    expect(host.textContent).toContain("Older schema · v6");
    expect(host.textContent).toContain("Inspect Missing project");
    expect(invoke.mock.calls).toEqual([
      ["backend_snapshot"], ["workspace_request", { method: "projects.list", params: { offset: 0 } }],
    ]);
  });

  it("inspects a missing project without writing recency", async () => {
    invoke.mockResolvedValueOnce(ready)
      .mockResolvedValueOnce({ projects: [missing], next_offset: null })
      .mockResolvedValueOnce({
        ...summary, root: missing.root, display_name: missing.display_name, exists: false,
        is_directory: false, initialized: false, storage: "ABSENT", schema_version: null, work_items: null,
      });
    await render();
    await click("Inspect Missing project");
    expect(host.querySelector(".project-result")?.textContent).toContain("Missing");
    expect(invoke.mock.lastCall).toEqual([
      "workspace_request", { method: "project.summary", params: { root: missing.root } },
    ]);
  });

  it("opens a project, shows canonical tree hierarchy and persists explicit updates via the bridge", async () => {
    let persisted: WorkItem | null = null;
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot") return ready;
      switch (payload.method) {
        case "projects.list": return { projects: [legacy], next_offset: null };
        case "project.open":
        case "project.summary":
          return { ...summary, work_items: persisted ? { total: 2, by_scope: { CHANGE: 1 }, by_status: { [persisted.status]: 1 } } : summary.work_items };
        case "work.tree":
          return { nodes: persisted ? [
            { ...item, work_item_id: "parent", objective: "Parent", depth: 0 },
            { ...persisted, parent_work_item_id: "parent", depth: 1 },
          ] : [], next_offset: null };
        case "work.create": persisted = item; return persisted;
        case "work.status": persisted = { ...item, status: "ACTIVE", allowed_transitions: ["COMPLETED"] }; return persisted;
        default: throw new Error("Unexpected method");
      }
    });
    await render();
    await click("Open Project");
    expect(host.textContent).toContain("Current · v7");
    expect(host.textContent).toContain("project_contract · project_rev");
    expect(host.textContent).toContain("workflow_contract · workflow_rev");
    await field("#work-objective", item.objective);
    await field("#work-scope", "CHANGE");
    await field("#work-criteria", "First criterion\nSecond criterion");
    await click("Create Work Item");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", {
      method: "work.create", params: {
        root: legacy.root, objective: item.objective, scope_level: "CHANGE",
        acceptance_criteria: ["First criterion", "Second criterion"],
      },
    }]);
    expect(host.querySelector('.work-tree li[data-depth="1"]')?.textContent).toContain("CHANGE · OPEN");
    await field("#work-status", "ACTIVE");
    await click("Update status");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", {
      method: "work.status", params: {
        root: legacy.root, work_item_id: item.work_item_id, expected_updated_at: item.updated_at, status: "ACTIVE",
      },
    }]);
    expect(host.querySelector('.work-tree li[data-depth="1"]')?.textContent).toContain("CHANGE · ACTIVE");
  });

  it("shows bounded failures and reconnects on explicit action", async () => {
    invoke.mockRejectedValueOnce("secret bridge detail")
      .mockResolvedValueOnce(ready)
      .mockRejectedValueOnce("secret registry detail");
    await render();
    expect(host.textContent).toContain("could not connect");
    await click("Reconnect");
    expect(host.textContent).toContain("Could not load projects");
    expect(host.textContent).not.toContain("secret");
    expect(invoke.mock.calls.filter(([command]) => command === "restart_backend")).toHaveLength(1);
    await act(async () => root.render(null));
    expect(release).toHaveBeenCalledOnce();
  });

  it("discards in-flight data when the sidecar exits", async () => {
    let respond: (value: ProjectSummary) => void = () => {};
    invoke.mockResolvedValueOnce(ready)
      .mockResolvedValueOnce({ projects: [legacy], next_offset: null })
      .mockReturnValueOnce(new Promise<ProjectSummary>((resolve) => { respond = resolve; }));
    await render();
    await click("Open Project");
    expect(host.textContent).toContain("Opening project");
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    await act(async () => respond(summary));
    expect(host.textContent).toContain("Disconnected");
    expect(host.textContent).not.toContain("Current · v7");
  });

  it("keeps controls unavailable when capabilities are absent", async () => {
    invoke.mockResolvedValueOnce({ ...ready, handshake: { ...ready.handshake, methods: ["future.method"] } });
    await render();
    expect(host.textContent).toContain("Project browsing is unavailable");
    expect(host.querySelector<HTMLInputElement>("#project-root")?.disabled).toBe(true);
    expect(invoke.mock.calls).toEqual([["backend_snapshot"]]);
  });
});

describe("desktop external repository import", () => {
  const digest = "a".repeat(64);
  const snapshot = "b".repeat(64);
  const repository = { ...legacy, root: "C:\\external", display_name: "external", schema_version: 9, storage: "CURRENT" as const };
  const discovery: ImportDiscovery = {
    root: repository.root, label: "DISCOVERED", head: "c".repeat(40), branch: "main",
    dirty: true, commit_allowed: true, existing_state: false, tracked_count: 5,
    languages: { Python: 2 }, manifests: [{ path: "pyproject.toml", ecosystem: "python" }],
    docs: ["<b>README.md</b>"], tests: ["tests/test_payments.py"],
    candidates: [{ path: "src", ecosystem: "python" }], exclusions: ["SENSITIVE_PATH"],
    ambiguities: [{
      code: "DIRTY_TRACKED", explanation: "Tracked content differs from HEAD.", required: true, options: ["CONFIRM"],
    }],
    discovery_digest: digest,
  };

  it("keeps inspection passive and imports only after explicit ambiguity confirmation", async () => {
    let imported = false;
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot") return { ...ready, handshake: {
        ...ready.handshake, methods: [...methods, "import.inspect", "import.commit"],
      } };
      switch (payload.method) {
        case "projects.list": return { projects: imported ? [repository] : [], next_offset: null };
        case "import.inspect": return discovery;
        case "import.commit":
          imported = true;
          return { root: discovery.root, status: "IMPORTED", label: "CONFIRMED", project_id: "project-1",
            discovery_digest: digest, snapshot_digest: snapshot, error: null };
        case "project.open": return { ...summary, root: discovery.root, schema_version: 9, display_name: "external",
          active_project: null, active_workflow: null };
        case "work.tree": return { nodes: [], next_offset: null };
        default: throw new Error("Unexpected method");
      }
    });
    await render();
    const panel = () => host.querySelector(".repository-import");
    expect(invoke.mock.calls.map(([_command, args]) => args?.method)).not.toContain("import.inspect");
    await field("#import-root", "C:\\external\\");
    await click("Inspect repository");
    expect(invoke.mock.calls).toEqual([
      ["backend_snapshot"],
      ["workspace_request", { method: "projects.list", params: { offset: 0 } }],
      ["workspace_request", { method: "import.inspect", params: { root: "C:\\external\\" } }],
    ]);
    expect(panel()?.textContent).toContain("DISCOVERED repository facts");
    expect(panel()?.textContent).toContain("<b>README.md</b>");
    expect(panel()?.querySelector("b")).toBeNull();
    await click("Import repository");
    expect(imported).toBe(false);
    await act(async () => panel()?.querySelector<HTMLInputElement>('input[type="checkbox"]')?.click());
    await field("#import-root", discovery.root);
    expect(panel()?.textContent).not.toContain("DISCOVERED repository facts");
    await click("Inspect repository");
    await click("Import repository");
    expect(imported).toBe(false);
    await act(async () => panel()?.querySelector<HTMLInputElement>('input[type="checkbox"]')?.click());
    await click("Import repository");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", { method: "import.commit", params: {
      root: discovery.root, discovery_digest: digest, confirmations: ["DIRTY_TRACKED"],
    } }]);
    expect(panel()?.querySelector(".import-confirmed")?.textContent).toContain(snapshot);
    expect(host.querySelector(".project-list")?.textContent).toContain("Open external");
    expect(host.querySelector(".project-result")?.textContent).not.toContain("Project Contract");
    expect(host.querySelector(".work-form")?.textContent).toContain("Create a Work Item");
  });
});

describe("desktop adaptive workflow", () => {
  const flowMethods = [
    ...methods, "scope.assess", "scope.set", "flow.status", "flow.start", "flow.complete", "work.report",
  ];
  const task = { ...item, scope_level: "TASK" };
  const project = { ...item, work_item_id: "work_2", scope_level: "PROJECT", objective: "Plan the project" };

  function flowFor(work: WorkItem, changes: Partial<FlowView> = {}): FlowView {
    const scope = work.scope_level as WorkScope;
    return {
      work_item_id: work.work_item_id, selected_scope: scope, recommended_scope: "TASK", minimum_required_scope: "TASK",
      policy_version: 1, flow_kind: scope, stage: scope === "PROJECT" ? "PLANNING_REQUIRED" : "READY",
      signals: ["NO_APPROVED_WORKFLOW"], unknowns: ["VALIDATION_POLICY_OPAQUE"],
      gates: [{ id: "VALIDATION", state: "NOT_VERIFIED" }], blockers: [],
      next_actions: scope === "PROJECT" ? ["Review existing Project Manager and Workflow planning."] : [],
      validation_state: "NOT_VERIFIED", validation_policy: null, workflow_ref: null, execution: null,
      latest_session: null, latest_projection: null, latest_handoff: null, provider_continuity: null, report_status: "NOT_VERIFIED",
      can_start: scope !== "PROJECT", can_complete: false, ...changes,
    };
  }

  function setup(initial: WorkItem, changes: Partial<FlowView> = {}) {
    let work = initial;
    let flow = flowFor(work, changes);
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot") return { ...ready, handshake: { ...ready.handshake, methods: flowMethods } };
      switch (payload.method) {
        case "projects.list": return { projects: [legacy], next_offset: null };
        case "project.open":
        case "project.summary": return { ...summary, schema_version: 9 };
        case "work.tree": return { nodes: [{ ...work, depth: 0 }], next_offset: null };
        case "work.get": return work;
        case "flow.status": return flow;
        case "scope.set":
          work = { ...work, scope_level: payload.params.scope_level, updated_at: "2026-02-02T00:00:00Z" };
          flow = flowFor(work);
          return { ...flow, updated_at: work.updated_at };
        case "flow.start":
          flow = flowFor(work, {
            stage: "EXECUTING", can_start: false,
            latest_session: { session_id: "session-1", provider: "codex", state: "RUNNING", run_generation: 1 },
            latest_projection: { projection_id: "projection-1", projection_digest: "a".repeat(64) },
          });
          return flow;
        case "work.report": return {
          work_item_id: work.work_item_id, flow, overall_status: "NOT_VERIFIED",
          objective: work.objective, acceptance_criteria: ["<b>Keep exact evidence.</b>"],
        };
        case "flow.complete":
          work = { ...work, status: "COMPLETED", updated_at: "2026-02-02T00:00:00Z" };
          flow = flowFor(work, { stage: "TERMINAL", can_start: false, validation_state: "VERIFIED" });
          return flow;
        default: throw new Error("Unexpected method");
      }
    });
  }

  async function pickWork(initial: WorkItem) {
    await render();
    await click("Open Project");
    await click(initial.objective);
  }

  const actions = () => invoke.mock.calls.map(([_command, payload]) => payload?.method);

  it("assesses a selected TASK passively and starts and reads the report only on explicit actions", async () => {
    setup(task);
    await pickWork(task);
    const panel = () => host.querySelector(".adaptive-workflow");
    expect(panel()?.textContent).toContain("Minimum required scopeTASK");
    expect(panel()?.textContent).toContain("StageREADY");
    expect(actions().filter((value) => value?.startsWith("flow."))).toEqual(["flow.status"]);
    await field("#adaptive-provider", "codex");
    expect(actions()).not.toContain("flow.start");
    await click("Start adaptive workflow");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", { method: "flow.start", params: {
      root: legacy.root, work_item_id: task.work_item_id, provider: "codex", timeout_seconds: 120, read_only: false,
    } }]);
    expect(panel()?.textContent).toContain("Latest projectionprojection-1");
    expect(panel()?.textContent).toContain("Latest sessionsession-1");
    expect(panel()?.textContent).not.toContain("Complete adaptive workflow");
    await click("Show Work Item report");
    expect(panel()?.querySelector(".adaptive-report")?.textContent).toContain("<b>Keep exact evidence.</b>");
    expect(panel()?.querySelector(".adaptive-report b")).toBeNull();
  });

  it("shows the PROJECT planning gate and sends a downgrade only after confirmation", async () => {
    setup(project);
    await pickWork(project);
    const panel = () => host.querySelector(".adaptive-workflow");
    expect(panel()?.textContent).toContain("StagePLANNING_REQUIRED");
    expect(panel()?.textContent).toContain("Project Manager");
    expect(panel()?.textContent).not.toContain("Start adaptive workflow");
    await field("#adaptive-scope", "CHANGE");
    await click("Update scope");
    expect(actions()).not.toContain("scope.set");
    const confirmation = panel()?.querySelector<HTMLInputElement>('input[type="checkbox"]');
    await act(async () => confirmation?.click());
    await click("Update scope");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", { method: "scope.set", params: {
      root: legacy.root, work_item_id: project.work_item_id, expected_updated_at: project.updated_at,
      scope_level: "CHANGE", confirm_downgrade: true,
    } }]);
    expect(panel()?.textContent).toContain("Selected scopeCHANGE");
  });

  it("completes only after the backend grants eligibility and the operator acts", async () => {
    const active = { ...task, status: "ACTIVE" };
    setup(active, { can_start: false, can_complete: true, stage: "COMPLETION_READY", validation_state: "VERIFIED" });
    await pickWork(active);
    expect(actions()).not.toContain("flow.complete");
    await click("Complete adaptive workflow");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", { method: "flow.complete", params: {
      root: legacy.root, work_item_id: active.work_item_id, expected_updated_at: active.updated_at,
    } }]);
    expect(host.querySelector(".adaptive-workflow")?.textContent).toContain("StageTERMINAL");
  });
});

describe("desktop Agents", () => {
  const sessionMethods = ["session.list", "session.get", "session.events", "session.start", "session.cancel", "session.recover"];
  const session: AgentSession = {
    session_id: "session_1", work_item_id: item.work_item_id, project_id: "project",
    provider: "codex", provider_version: null, model: null, state: "RUNNING", run_generation: 1,
    started_at: "2026-01-02T00:00:00Z", created_at: "2026-01-02T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z", finished_at: null,
    input_tokens: 0, output_tokens: null, cached_tokens: null, duration_ms: null,
    reported_cost: null, reported_cost_currency: null, timeout_seconds: 120,
    read_only: false, can_cancel: true, can_recover: false, cleanup_unconfirmed: false,
  };

  function setup(initial: AgentSession[], switchRequest?: (method: string) => SwitchView) {
    const rows = initial.slice();
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot") {
        if (!switchRequest) return { ...ready, handshake: { ...ready.handshake, methods: [...methods, ...sessionMethods] } };
        const [codex] = ready.providers.providers;
        return { ...ready, handshake: { ...ready.handshake, methods: [...methods, ...sessionMethods, "switch.status", "switch.execute"] },
          providers: { providers: [codex, { ...codex, provider: "claude" }] } };
      }
      switch (payload.method) {
        case "switch.status":
        case "switch.execute": return switchRequest?.(payload.method);
        case "projects.list": return { projects: [legacy], next_offset: null };
        case "project.open": return { ...summary, schema_version: 8 };
        case "work.tree": return { nodes: [{ ...item, depth: 0 }], next_offset: null };
        case "session.list": return { sessions: rows.slice(), next_offset: null };
        case "session.get": return rows.find((row) => row.session_id === payload.params.session_id);
        case "session.events": return {
          events: payload.params.after_sequence ? [] : [{
            sequence: 1, occurred_at: session.created_at, kind: "OUTPUT",
            payload: { text: "<b>Provider output</b>" }, native_kind: "item.completed",
          }], next_after_sequence: payload.params.after_sequence || 1,
        };
        case "session.start": rows.unshift(session); return session;
        case "session.cancel":
          rows[0] = { ...session, state: "CANCELLED", can_cancel: false };
          return rows[0];
        case "session.recover": {
          const index = rows.findIndex((row) => row.session_id === payload.params.session_id);
          rows[index] = { ...rows[index], state: "STARTING", can_recover: false };
          return rows[index];
        }
        default: throw new Error("Unexpected method");
      }
    });
  }

  const requested = () => invoke.mock.calls.map(([_command, payload]) => payload?.method);

  it("reads list, detail, and normalized events without probing providers", async () => {
    setup([session]);
    await render();
    await click("Open Project");
    expect(requested()).not.toContain("session.list");
    await click("Agents");
    expect(host.querySelector(".session-list")?.textContent).toContain("RUNNING");
    await click(session.session_id);
    expect(host.querySelector(".session-detail")?.textContent).toContain("Input tokens0");
    expect(host.querySelector(".session-detail")?.textContent).toContain("ModelUNKNOWN");
    expect(host.querySelector(".session-events")?.textContent).toContain("<b>Provider output</b>");
    expect(host.querySelector(".session-events b")).toBeNull();
    expect(requested().filter((method) => method?.startsWith("session."))).toEqual([
      "session.list", "session.get", "session.events",
    ]);
    expect(requested().some((method) => method?.startsWith("provider"))).toBe(false);
  });

  it("starts and cancels explicitly through the Rust bridge", async () => {
    setup([]);
    await render();
    await click("Open Project");
    await click("Agents");
    expect(host.textContent).toContain("No Agent Sessions");
    await field("#session-work", item.work_item_id);
    await field("#session-provider", "codex");
    await field("#session-timeout", "3");
    await field("#session-instruction", "Write a unit test.");
    await click("Start session");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", {
      method: "session.start", params: {
        root: legacy.root, provider: "codex", work_item_id: item.work_item_id,
        timeout_seconds: 3, read_only: false, instruction: "Write a unit test.",
      },
    }]);
    await click("Cancel session");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", {
      method: "session.cancel", params: { root: legacy.root, session_id: session.session_id, generation: 1 },
    }]);
    expect(host.querySelector(".session-detail")?.textContent).toContain("StateCANCELLED");
    expect(host.textContent).not.toContain("Cancel session");
  });

  it("offers only eligible recovery and can bind original instructions", async () => {
    const interrupted = { ...session, state: "INTERRUPTED", can_cancel: false };
    const recoverable = { ...interrupted, session_id: "recoverable", can_recover: true };
    setup([interrupted, recoverable]);
    await render();
    await click("Open Project");
    await click("Agents");
    await click(session.session_id);
    expect(host.textContent).toContain("Native recovery is unavailable");
    expect(host.textContent).not.toContain("Recover session");
    await click(recoverable.session_id);
    await field("#session-recovery-instruction", "Original instruction");
    await click("Recover session");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", {
      method: "session.recover", params: {
        root: legacy.root, session_id: recoverable.session_id, instruction: "Original instruction",
      },
    }]);
    expect(host.querySelector(".session-detail")?.textContent).toContain("StateSTARTING");
  });

  function switchView(changes: Partial<SwitchView> = {}): SwitchView {
    return {
      switch_key: "a".repeat(64), project_id: "project", work_item_id: item.work_item_id,
      source_session_id: session.session_id, source_provider: "codex", source_generation: 1,
      source_state: "RUNNING", cleanup_unconfirmed: false, target_provider: "claude",
      stage: "SOURCE_ACTIVE", handoff_id: null, handoff_digest: null, projection_id: null,
      projection_digest: null, target_session_id: null, target_state: null, blocker: null,
      ...changes,
    };
  }

  it("keeps target selection passive and reports an unavailable target without stopping the source", async () => {
    setup([session], (method) => method === "switch.execute"
      ? switchView({ stage: "BLOCKED", blocker: "TARGET_UNAVAILABLE" })
      : switchView());
    await render();
    await click("Open Project");
    await click("Agents");
    await click(session.session_id);
    const select = host.querySelector<HTMLSelectElement>("#switch-target-provider")!;
    expect(Array.from(select.options, (option) => option.value)).toEqual(["", "claude"]);
    expect(select.value).toBe("");
    expect(requested()).not.toContain("switch.status");
    await field("#switch-target-provider", "claude");
    expect(requested().filter((method) => method?.startsWith("switch."))).toEqual(["switch.status"]);
    await click("Switch provider");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", { method: "switch.execute", params: {
      root: legacy.root, source_session_id: session.session_id, target_provider: "claude", source_generation: 1,
    } }]);
    expect(host.querySelector(".provider-switch")?.textContent).toContain("The selected target provider is unavailable.");
    expect(host.querySelector(".provider-switch")?.textContent).toContain("Source stateRUNNING");
    expect(requested()).not.toContain("session.cancel");
    expect(requested().some((method) => method?.startsWith("providers."))).toBe(false);
  });

  it("shows backend switch stages and exact continuity before replaying the same target", async () => {
    vi.useFakeTimers();
    try {
      let view = switchView();
      let executions = 0;
      setup([session], (method) => {
        if (method !== "switch.execute") return view;
        executions += 1;
        if (executions === 1) view = switchView({ stage: "STOPPING_SOURCE", source_state: "CANCELLING" });
        if (executions === 2) view = { ...view, stage: "PROJECTION_READY", projection_id: "projection-b",
          projection_digest: "b".repeat(64) };
        if (executions === 3) view = { ...view, stage: "TARGET_SESSION_ACTIVE",
          target_session_id: "session-2", target_state: "RUNNING" };
        return view;
      });
      await render();
      await click("Open Project");
      await click("Agents");
      await click(session.session_id);
      await field("#switch-target-provider", "claude");
      await click("Switch provider");
      const panel = host.querySelector(".provider-switch")!;
      expect(panel.querySelector(".switch-stage")?.textContent).toBe("STOPPING_SOURCE");
      expect(panel.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled).toBe(true);
      view = switchView({ stage: "HANDOFF_READY", source_state: "CANCELLED",
        handoff_id: "handoff-a", handoff_digest: "a".repeat(64) });
      await act(async () => vi.advanceTimersByTimeAsync(1000));
      expect(panel.querySelector(".switch-stage")?.textContent).toBe("HANDOFF_READY");
      expect(panel.textContent).toContain("handoff-a");
      expect(executions).toBe(1);
      await click("Continue switch");
      expect(panel.querySelector(".switch-stage")?.textContent).toBe("PROJECTION_READY");
      expect(panel.textContent).toContain("projection-b");
      await click("Continue switch");
      expect(panel.querySelector(".switch-stage")?.textContent).toBe("TARGET_SESSION_ACTIVE");
      expect(panel.textContent).toContain("session-2");
      await click("Replay switch");
      expect(panel.textContent).toContain("session-2");
      expect(panel.textContent).toContain("Existing target session confirmed.");
      expect(executions).toBe(4);
    } finally {
      vi.useRealTimers();
    }
  });

  it("opens the target session even when the Agents background refresh is in flight", async () => {
    vi.useFakeTimers();
    try {
      const target: AgentSession = {
        ...session, session_id: "session-2", provider: "claude", state: "SUCCEEDED", can_cancel: false,
      };
      setup([session, target], () => switchView({
        stage: "TARGET_SESSION_TERMINAL", source_state: "CANCELLED",
        target_session_id: target.session_id, target_state: "SUCCEEDED",
      }));
      await render();
      await click("Open Project");
      await click("Agents");
      await click(session.session_id);
      await field("#switch-target-provider", "claude");

      let finishPoll: (value: { sessions: AgentSession[]; next_offset: null }) => void = () => {};
      invoke.mockReturnValueOnce(new Promise((resolve) => { finishPoll = resolve; }));
      await act(async () => vi.advanceTimersByTimeAsync(1000));
      expect(invoke.mock.lastCall?.[1]?.method).toBe("session.list");

      await click("Open target session");
      expect(host.querySelector<HTMLButtonElement>(".agents > .section-heading button")?.disabled).toBe(true);
      expect(host.querySelector(".session-detail h4")?.textContent).toBe(session.session_id);
      await act(async () => finishPoll({ sessions: [session, target], next_offset: null }));
      expect(host.querySelector(".session-detail h4")?.textContent).toBe(target.session_id);
      expect(host.querySelector(".session-detail")?.textContent).toContain("StateSUCCEEDED");
      expect(invoke.mock.calls).toContainEqual(["workspace_request", {
        method: "session.events",
        params: { root: legacy.root, session_id: target.session_id, after_sequence: 0, limit: 10 },
      }]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows bounded failures and unmounts on disconnect", async () => {
    setup([]);
    await render();
    await click("Open Project");
    let reject: (error: Error) => void = () => {};
    invoke.mockReturnValueOnce(new Promise((_resolve, rejectPromise) => { reject = rejectPromise; }));
    await click("Agents");
    expect(host.textContent).toContain("Loading Agent Sessions");
    await act(async () => reject(new Error("private provider error")));
    expect(host.textContent).toContain("Could not load the Agents view");
    expect(host.textContent).not.toContain("private provider error");
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    expect(host.textContent).toContain("Disconnected");
    expect(host.querySelector(".agents")).toBeNull();
  });
});

describe("desktop Context audit", () => {
  const contextMethods = [
    "context.list", "context.create", "context.get", "context.sources", "context.verify",
    "handoff.list", "handoff.create", "handoff.get", "session.list",
  ];
  const projection: ContextProjection = {
    projection_id: "projection-1", project_id: "project", work_item_id: item.work_item_id,
    target_provider: "codex", selector_policy_version: 1, redaction_policy_version: 1,
    projection_digest: "a".repeat(64), source_set_digest: "b".repeat(64), created_at: item.updated_at,
  };
  const handoff: SessionHandoff = {
    handoff_id: "handoff-1", project_id: "project", work_item_id: item.work_item_id,
    source_session_id: "session-finished", run_generation: 2, event_cutoff: 14,
    provider: "codex", terminal_state: "CANCELLED", digest: "c".repeat(64), created_at: item.updated_at,
  };

  function setup(invalid = false) {
    let createdHandoff = false;
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot") {
        return { ...ready, handshake: { ...ready.handshake, methods: [...methods, ...contextMethods] } };
      }
      switch (payload.method) {
        case "projects.list": return { projects: [legacy], next_offset: null };
        case "project.open": return { ...summary, schema_version: 9 };
        case "work.tree": return { nodes: [{ ...item, objective: "secret objective must not render", depth: 0 }], next_offset: null };
        case "session.list": return { sessions: [], next_offset: null };
        case "context.list": return { projections: [projection], next_offset: null };
        case "context.create":
        case "context.get": return projection;
        case "context.sources": return { sources: [{
          ordinal: 0, kind: "WORK_ITEM", authority: "OPERATIONAL", identity: item.work_item_id,
          revision: item.updated_at, digest: "d".repeat(64), reason: "WORK_ITEM",
          redaction: ["CREDENTIAL_ASSIGNMENT"], capture: "secret that must not render",
        }], next_offset: null };
        case "context.verify":
          if (invalid) throw new Error("secret rejected detail");
          return { ...projection, verified: true };
        case "handoff.list": return { handoffs: createdHandoff ? [handoff] : [], next_offset: null };
        case "handoff.create": createdHandoff = true;
        case "handoff.get": return { ...handoff, payload: { note: "secret that must not render" } };
        default: throw new Error("Unexpected method");
      }
    });
  }

  it("audits exact provenance, redaction and digest without exposing captures or probing providers", async () => {
    setup();
    await render();
    await click("Open Project");
    expect(invoke.mock.calls.some(([, payload]) => payload?.method === "context.list")).toBe(false);
    await click("Context");
    await click(projection.projection_id);
    expect(host.querySelector(".context-sources")?.textContent).toContain("WORK_ITEM · OPERATIONAL");
    expect(host.querySelector(".context-sources")?.textContent).toContain("CREDENTIAL_ASSIGNMENT");
    expect(host.querySelector(".context-sources")?.textContent).toContain(item.updated_at);
    expect(host.textContent).not.toContain("secret");
    await click("Verify projection");
    expect(host.querySelector(".projection-detail [role=status]")?.textContent).toBe("Verified");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", {
      method: "context.verify", params: { root: legacy.root, projection_id: projection.projection_id },
    }]);
    expect(invoke.mock.calls.some(([, payload]) => payload?.method?.startsWith("providers."))).toBe(false);
  });

  it("creates a projection and handoff through explicit RPCs and displays only handoff provenance", async () => {
    setup();
    await render();
    await click("Open Project");
    await click("Context");
    await field("#context-work", item.work_item_id);
    await field("#context-provider", "codex");
    await click("Create projection");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", {
      method: "context.create", params: { root: legacy.root, work_item_id: item.work_item_id, provider: "codex" },
    }]);
    await field("#handoff-session", handoff.source_session_id);
    await click("Create handoff");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", {
      method: "handoff.create", params: { root: legacy.root, session_id: handoff.source_session_id },
    }]);
    expect(host.querySelector(".handoff-detail")?.textContent).toContain("Event cutoff14");
    expect(host.querySelector(".handoff-detail")?.textContent).toContain("AuthorityDERIVED");
    expect(host.textContent).not.toContain("secret");
  });

  it("reports a verification failure without displaying backend error data", async () => {
    setup(true);
    await render();
    await click("Open Project");
    await click("Context");
    await click(projection.projection_id);
    await click("Verify projection");
    expect(host.querySelector(".projection-detail [role=status]")?.textContent).toBe("Verification failed");
    expect(host.textContent).not.toContain("secret rejected detail");
  });
});
