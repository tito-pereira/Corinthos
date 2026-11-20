// SPDX-License-Identifier: MPL-2.0
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { AgentSession, BackendSnapshot, CommandProjectCard, ContextProjection, FlowView, ImportDiscovery, ImportResult, ProjectEntry, ProjectInspection, ProjectSummary, ReviewDetails, SessionHandoff, SwitchView, WorkItem, WorkScope } from "./bridge";

const { invoke, listen } = vi.hoisted(() => ({ invoke: vi.fn(), listen: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen }));

const methods = ["projects.list", "project.summary", "project.open", "project.inspect", "project.open_checked", "work.tree", "work.get", "work.create", "work.status"];
const currentInspection = (root: string) => ({ root, status: "CURRENT", reason: "Verified current project.",
  can_import: false, schema_version: 9, inspection_digest: "e".repeat(64) });
const ready: Extract<BackendSnapshot, { connection: "ready" }> = {
  connection: "ready",
  desktop: { version: "0.1.0", identifier: "org.corinthos.desktop" },
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
  if (target.matches(":disabled") || target.closest("details:not([open])")) {
    throw new Error(`Input unavailable: ${id}`);
  }
  const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(target), "value")?.set;
  await act(async () => {
    setter?.call(target, value);
    target.dispatchEvent(new Event(target instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}

async function openManualPath(id: string) {
  const details = host.querySelector(id)?.closest("details");
  if (!details) throw new Error(`Manual path disclosure missing: ${id}`);
  if (!details.open) await act(async () => details.querySelector("summary")!.click());
  expect(details.open).toBe(true);
}

async function openDiagnostics() {
  const details = host.querySelector<HTMLDetailsElement>("details.diagnostics");
  if (!details) throw new Error("Diagnostics disclosure missing");
  if (!details.open) await act(async () => {
    details.querySelector("summary")!.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  expect(details.open).toBe(true);
}

function diagnostic(label: string) {
  return Array.from(host.querySelectorAll(".diagnostics dt"))
    .find((term) => term.textContent === label)?.nextElementSibling?.textContent;
}

describe("desktop Home and Projects", () => {
  it("loads known and missing projects passively once the backend negotiates v2", async () => {
    let respond: (value: BackendSnapshot) => void = () => {};
    invoke.mockReturnValueOnce(new Promise<BackendSnapshot>((resolve) => { respond = resolve; }));
    invoke.mockResolvedValueOnce({ projects: [legacy, missing], next_offset: null });
    await render();
    expect(host.textContent).toContain("Connecting");
    const navigation = host.querySelector(".app-sidebar .primary-navigation");
    expect(Array.from(navigation!.querySelectorAll("button"), (button) => button.textContent))
      .toEqual(["Home", "Command", "Projects", "Settings"]);
    expect(Array.from(navigation!.querySelectorAll("button")).every((button) => button.disabled)).toBe(true);
    await click("Projects");
    expect(invoke.mock.calls).toEqual([["backend_snapshot"]]);
    await act(async () => respond(ready));
    expect(host.querySelector(".workspace-content .primary-navigation")).toBeNull();
    expect(Array.from(host.querySelectorAll<HTMLButtonElement>(".primary-navigation button"))
      .every((button) => !button.disabled)).toBe(true);
    expect(host.textContent).not.toContain("0.1.0");
    expect(host.querySelector("#backend-title")).toBeNull();
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
        case "project.inspect": return currentInspection(payload.params.root);
        case "projects.list": return { projects: [legacy], next_offset: null };
        case "project.open_checked":
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
    await click("Work");
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
      .mockResolvedValueOnce(currentInspection(legacy.root))
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
    expect(host.querySelector<HTMLButtonElement>('[aria-pressed="false"]')?.disabled).toBe(true);
    expect(invoke.mock.calls).toEqual([["backend_snapshot"]]);
  });
});

describe("backend status and diagnostics", () => {
  const runtime = { python_implementation: "CPython", python_version: "3.14.10", supported_schema_version: 9 };
  const diagnosticReady = { ...ready, handshake: { ...ready.handshake, methods: [...methods, "system.diagnostics"] } };

  it("shares the Corinthos brand without duplicating its accessible name", async () => {
    invoke.mockResolvedValueOnce(ready).mockResolvedValueOnce({ projects: [], next_offset: null });
    await render();
    const heading = host.querySelector("h1");
    const mastheadLogo = heading?.querySelector("img");
    expect(heading?.closest(".app-sidebar")).not.toBeNull();
    expect(host.querySelector(".app-sidebar .brand-symbol")?.getAttribute("src"))
      .toContain("corinthos-symbol.svg");
    expect(host.querySelector(".app-sidebar .connection-control")).not.toBeNull();
    expect(heading?.textContent).toBe("Corinthos");
    expect(mastheadLogo?.getAttribute("alt")).toBe("");
    expect(mastheadLogo?.getAttribute("src")).toContain("corinthos-logo.svg");
    await click("Settings");
    await openDiagnostics();
    const aboutLogo = host.querySelector(".diagnostics .brand-logo");
    expect(aboutLogo?.getAttribute("src")).toBe(mastheadLogo?.getAttribute("src"));
    expect(aboutLogo?.getAttribute("alt")).toBe("");
    expect(invoke.mock.calls).toHaveLength(2);
  });

  it("reads runtime details only through explicit Settings diagnostics", async () => {
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot") return diagnosticReady;
      if (payload.method === "projects.list") return { projects: [], next_offset: null };
      if (payload.method === "system.diagnostics") return runtime;
      throw new Error("Unexpected operation");
    });
    await render();
    expect(host.querySelector("#backend-title")).toBeNull();
    expect(host.textContent).not.toContain("Application version");
    expect(invoke.mock.calls).toHaveLength(2);
    await click("Settings");
    expect(host.textContent).not.toContain("Application version");
    await openDiagnostics();
    expect(diagnostic("Application version")).toBe("0.1.0");
    expect(diagnostic("Desktop version")).toBe("0.1.0");
    expect(diagnostic("RPC protocol")).toBe("v2");
    expect(diagnostic("Private runtime")).toBe("CPython 3.14.10");
    expect(diagnostic("Supported storage schema")).toBe("v9");
    expect(diagnostic("Selected project schema")).toBe("No project selected");
    expect(diagnostic("Desktop identifier")).toBe("org.corinthos.desktop");
    expect(invoke.mock.calls.filter(([command]) => command === "backend_snapshot")).toHaveLength(1);
    expect(invoke.mock.calls.filter(([, payload]) => payload?.method === "system.diagnostics")).toEqual([
      ["workspace_request", { method: "system.diagnostics", params: {} }],
    ]);
  });

  it("offers native identity and unavailable runtime diagnostics after startup failure", async () => {
    invoke.mockResolvedValueOnce({ connection: "unavailable", desktop: {
      version: "2.3.4", identifier: "org.fixture.desktop",
    }, failure_origin: "startup", error: "The private runtime is missing." });
    await render();
    expect(host.querySelector(".connection")?.textContent).toBe("Backend unavailable");
    expect(host.textContent).toContain("The private runtime is missing.");
    await openDiagnostics();
    expect(diagnostic("Desktop version")).toBe("2.3.4");
    expect(diagnostic("Desktop identifier")).toBe("org.fixture.desktop");
    expect(diagnostic("Application version")).toBe("Unavailable");
    expect(diagnostic("Private runtime")).toBe("Unavailable");
    expect(invoke.mock.calls).toEqual([["backend_snapshot"]]);
    expect(Array.from(host.querySelectorAll("button")).some((button) => button.textContent === "Reconnect")).toBe(true);
  });

  it("keeps unsupported runtime details unavailable without diagnostic polling", async () => {
    invoke.mockResolvedValueOnce(ready).mockResolvedValueOnce({ projects: [], next_offset: null });
    await render();
    await click("Settings");
    await openDiagnostics();
    expect(diagnostic("Private runtime")).toBe("Unavailable");
    expect(diagnostic("Supported storage schema")).toBe("Unavailable");
    expect(invoke.mock.calls).toHaveLength(2);
  });

  it.each([
    { storage: "CURRENT" as const, schema_version: 9, label: "v9" },
    { storage: "ABSENT" as const, schema_version: null, label: "No database" },
  ])("distinguishes supported schema from selected project $label", async ({ storage, schema_version, label }) => {
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot") return diagnosticReady;
      switch (payload.method) {
        case "projects.list": return { projects: [{ ...legacy, storage, schema_version }], next_offset: null };
        case "project.inspect": return { ...currentInspection(legacy.root), schema_version };
        case "project.open_checked": return { ...summary, storage, schema_version };
        case "system.diagnostics": return runtime;
        default: throw new Error(`Unexpected operation ${payload.method}`);
      }
    });
    await render();
    await click("Open Project");
    await click("Settings");
    await openDiagnostics();
    expect(diagnostic("Supported storage schema")).toBe("v9");
    expect(diagnostic("Selected project schema")).toBe(label);
  });

  it("lets a disconnect invalidate the initial delayed ready response", async () => {
    let finish: (value: BackendSnapshot) => void = () => {};
    invoke.mockReturnValueOnce(new Promise<BackendSnapshot>((resolve) => { finish = resolve; }));
    await render();
    expect(host.querySelector(".connection")?.textContent).toBe("Connecting…");
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    await act(async () => finish(ready));
    expect(host.querySelector(".connection")?.textContent).toBe("Disconnected");
    expect(Array.from(host.querySelectorAll<HTMLButtonElement>(".primary-navigation button")))
      .toHaveLength(4);
    expect(Array.from(host.querySelectorAll<HTMLButtonElement>(".primary-navigation button"))
      .every((button) => button.disabled)).toBe(true);
    expect(invoke.mock.calls).toEqual([["backend_snapshot"]]);
  });

  it("guards duplicate restarts and rejects ready replies superseded by disconnect", async () => {
    let finish: (value: BackendSnapshot) => void = () => {};
    invoke.mockImplementation((command, payload) => {
      if (command === "backend_snapshot") return Promise.resolve(ready);
      if (command === "restart_backend") return new Promise<BackendSnapshot>((resolve) => { finish = resolve; });
      if (payload.method === "projects.list") return Promise.resolve({ projects: [], next_offset: null });
      throw new Error("Unexpected operation");
    });
    await render();
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    const reconnect = Array.from(host.querySelectorAll("button")).find((button) => button.textContent === "Reconnect")!;
    await act(async () => { reconnect.click(); reconnect.click(); });
    expect(host.querySelector(".connection")?.textContent).toBe("Restarting…");
    expect(invoke.mock.calls.filter(([command]) => command === "restart_backend")).toHaveLength(1);
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    await act(async () => finish(ready));
    expect(host.querySelector(".connection")?.textContent).toBe("Disconnected");
    expect(Array.from(host.querySelectorAll<HTMLButtonElement>(".primary-navigation button"))
      .every((button) => button.disabled)).toBe(true);
    invoke.mockResolvedValueOnce({ connection: "unavailable", desktop: ready.desktop,
      failure_origin: "startup", error: "The private runtime could not start." });
    await click("Reconnect");
    expect(host.querySelector(".connection")?.textContent).toBe("Backend unavailable");
    expect(host.textContent).toContain("The private runtime could not start.");
    invoke.mockResolvedValueOnce(ready).mockResolvedValueOnce({ projects: [], next_offset: null });
    await click("Reconnect");
    expect(host.querySelector(".connection")?.textContent).toBe("Connected");
  });

  it("refreshes selected project and Work Item through passive reads after reconnect", async () => {
    let changed = false;
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot" || command === "restart_backend") return ready;
      switch (payload.method) {
        case "projects.list": return { projects: [{ ...legacy, storage: "CURRENT", schema_version: 9 }], next_offset: null };
        case "project.inspect": return currentInspection(legacy.root);
        case "project.open_checked": return { ...summary, schema_version: 9 };
        case "project.summary": return { ...summary, schema_version: 9 };
        case "work.tree": return { nodes: [{ ...item, depth: 0 }], next_offset: null };
        case "work.get": return { ...item, objective: changed ? "Refreshed Work Item" : item.objective };
        default: throw new Error(`Unexpected operation ${payload.method}`);
      }
    });
    await render();
    await click("Open Project");
    await click("Work");
    await click(item.objective);
    expect(host.querySelector(".work-selection")?.textContent).toContain(item.objective);
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    changed = true;
    await click("Reconnect");
    expect(host.querySelector('.primary-navigation [aria-current="page"]')?.textContent).toBe("Projects");
    expect(host.querySelector('.project-views [aria-pressed="true"]')?.textContent).toBe("Work");
    expect(host.querySelector(".work-selection")?.textContent).toContain("Refreshed Work Item");
    expect(invoke.mock.calls.filter(([, payload]) => payload?.method === "project.open_checked")).toHaveLength(1);
    expect(invoke.mock.calls.filter(([, payload]) => payload?.method === "project.summary")).toHaveLength(1);
    expect(invoke.mock.calls.filter(([, payload]) => payload?.method === "project.create" || payload?.method === "import.commit")).toHaveLength(0);
  });

  it("restores the latest Work selection after entering through Command attention", async () => {
    const other = { ...item, work_item_id: "other-work", objective: "Other Work Item" };
    const card: CommandProjectCard = {
      root: summary.root, display_name: summary.display_name, last_accessed_at: null,
      availability: "AVAILABLE", initialized: true, storage: "CURRENT", schema_version: 9,
      project_id: "project", error: null, active_project: null, active_workflow: null,
      import_status: null, external_import: null, work_counts: null, focus_work_items: [],
      sessions: null, latest_context: null, lifecycle: null,
      validation: { state: "NOT_VERIFIED", latest_run: null, active_plan_refs: [], outcome: null },
      attention: [{ kind: "BLOCKED", owner_type: "WORK_ITEM", owner_id: item.work_item_id,
        reason_code: "BLOCKED", severity: "ACTION", summary: "Inspect Work", navigation: "WORK", action_ids: [] }],
    };
    const linkedReady = { ...ready, handshake: { ...ready.handshake, methods: [...methods, "command.overview"] } };
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot" || command === "restart_backend") return linkedReady;
      switch (payload.method) {
        case "projects.list": return { projects: [], next_offset: null };
        case "command.overview": return { projects: [card], next_offset: null };
        case "project.summary": return { ...summary, schema_version: 9 };
        case "work.tree": return { nodes: [item, other].map((value) => ({ ...value, depth: 0 })), next_offset: null };
        case "work.get": return payload.params.work_item_id === other.work_item_id ? other : item;
        default: throw new Error(`Unexpected operation ${payload.method}`);
      }
    });
    await render();
    await click("Command");
    await click("Open Attention");
    expect(host.querySelector(".work-selection code")?.textContent).toBe(item.work_item_id);
    await click(other.objective);
    expect(host.querySelector(".work-selection code")?.textContent).toBe(other.work_item_id);
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    await click("Reconnect");
    expect(host.querySelector('.project-views [aria-pressed="true"]')?.textContent).toBe("Work");
    expect(host.querySelector(".work-selection code")?.textContent).toBe(other.work_item_id);
    expect(invoke.mock.calls.filter(([, payload]) => payload?.method === "work.get")
      .map(([, payload]) => payload.params.work_item_id)).toEqual([item.work_item_id, other.work_item_id, other.work_item_id]);
  });

  it("rejects a reconnect summary superseded by an explicit open of the same project", async () => {
    let finish: (value: ProjectSummary) => void = () => {};
    let finishOpen: (value: ProjectSummary) => void = () => {};
    let opened = 0;
    invoke.mockImplementation((command, payload) => {
      if (command === "backend_snapshot" || command === "restart_backend") return Promise.resolve(ready);
      switch (payload.method) {
        case "projects.list": return Promise.resolve({ projects: [{ ...legacy, storage: "CURRENT", schema_version: 9 }], next_offset: null });
        case "project.inspect": return Promise.resolve(currentInspection(legacy.root));
        case "project.open_checked": return ++opened === 1 ? Promise.resolve({ ...summary, schema_version: 9 })
          : new Promise<ProjectSummary>((resolve) => { finishOpen = resolve; });
        case "project.summary": return new Promise<ProjectSummary>((resolve) => { finish = resolve; });
        default: throw new Error(`Unexpected operation ${payload.method}`);
      }
    });
    await render();
    await click("Open Project");
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    await click("Reconnect");
    expect(host.textContent).toContain("Refreshing selected project");
    await click("Open Project");
    await act(async () => finish({ ...summary, schema_version: 9, display_name: "Stale project" }));
    try {
      expect(host.querySelector(".project-result")).toBeNull();
      expect(host.textContent).not.toContain("Stale project");
    } finally {
      await act(async () => finishOpen({ ...summary, schema_version: 9, display_name: "Newly opened project" }));
    }
    expect(host.querySelector(".project-result")?.textContent).toContain("Newly opened project");
    expect(host.textContent).not.toContain("Stale project");
  });

  it("retains the established Work selection when an older pick finishes after disconnect", async () => {
    const other = { ...item, work_item_id: "other-work", objective: "Other Work Item" };
    let finish: (value: WorkItem) => void = () => {};
    let delayed = true;
    invoke.mockImplementation((command, payload) => {
      if (command === "backend_snapshot" || command === "restart_backend") return Promise.resolve(ready);
      switch (payload.method) {
        case "projects.list": return Promise.resolve({ projects: [{ ...legacy, storage: "CURRENT", schema_version: 9 }], next_offset: null });
        case "project.inspect": return Promise.resolve(currentInspection(legacy.root));
        case "project.open_checked":
        case "project.summary": return Promise.resolve({ ...summary, schema_version: 9 });
        case "work.tree": return Promise.resolve({ nodes: [item, other].map((value) => ({ ...value, depth: 0 })), next_offset: null });
        case "work.get": return payload.params.work_item_id === other.work_item_id
          ? delayed ? new Promise<WorkItem>((resolve) => { finish = resolve; }) : Promise.resolve(other)
          : Promise.resolve(item);
        default: throw new Error(`Unexpected operation ${payload.method}`);
      }
    });
    await render();
    await click("Open Project");
    await click("Work");
    await click(item.objective);
    await click(other.objective);
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    await act(async () => finish(other));
    delayed = false;
    await click("Reconnect");
    expect(host.querySelector(".work-selection")?.textContent).toContain(item.objective);
    expect(host.querySelector(".work-selection")?.textContent).not.toContain(other.objective);
    expect(invoke.mock.calls.filter(([, payload]) => payload?.method === "work.get").at(-1)?.[1].params.work_item_id)
      .toBe(item.work_item_id);
  });

  it("ignores a diagnostic response from the disconnected backend", async () => {
    let finish: (value: typeof runtime) => void = () => {};
    let calls = 0;
    invoke.mockImplementation((command, payload) => {
      if (command === "backend_snapshot" || command === "restart_backend") return Promise.resolve(diagnosticReady);
      if (payload.method === "projects.list") return Promise.resolve({ projects: [], next_offset: null });
      if (payload.method === "system.diagnostics") return ++calls === 1
        ? new Promise<typeof runtime>((resolve) => { finish = resolve; }) : Promise.resolve(runtime);
      throw new Error("Unexpected operation");
    });
    await render();
    await click("Settings");
    await openDiagnostics();
    expect(host.textContent).toContain("Reading runtime diagnostics");
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    await act(async () => finish({ ...runtime, python_version: "99.99.99" }));
    expect(diagnostic("Private runtime")).toBe("Unavailable");
    expect(host.textContent).not.toContain("99.99.99");
    await click("Reconnect");
    expect(host.querySelector('.primary-navigation [aria-current="page"]')?.textContent).toBe("Settings");
    expect(diagnostic("Private runtime")).toBe("CPython 3.14.10");
    expect(calls).toBe(2);
  });
});

describe("distinct project onboarding", () => {
  function setup(inspect: () => Promise<ProjectInspection> = async () => currentInspection(legacy.root) as ProjectInspection,
    opened: () => Promise<ProjectSummary> = async () => summary) {
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot") return { ...ready, handshake: {
        ...ready.handshake, methods: [...methods, "project.create", "import.inspect", "import.status"],
      } };
      switch (payload.method) {
        case "projects.list": return { projects: [], next_offset: null };
        case "project.inspect": return inspect();
        case "project.open_checked": return opened();
        default: throw new Error("Unexpected method");
      }
    });
  }

  async function inspectPath() {
    await click("Open existing project");
    await openManualPath("#project-root");
    await field("#project-root", legacy.root);
    await click("Open project");
  }

  const mutations = () => invoke.mock.calls.filter(([, args]) =>
    ["project.create", "project.open", "project.open_checked", "import.commit"].includes(args?.method));

  it("offers three explained routes and mounts only the selected form before recents", async () => {
    setup();
    await render();
    expect(host.querySelectorAll("form")).toHaveLength(0);
    expect(host.textContent).toContain("Choose Create new project, Open existing project or Import Git repository above");
    expect(host.querySelectorAll(".onboarding-choices p")).toHaveLength(3);
    await click("Open existing project");
    expect(host.querySelector('form[aria-label="Open project"]')).not.toBeNull();
    await click("Import Git repository");
    expect(host.querySelectorAll("form")).toHaveLength(1);
    expect(host.querySelector('form[aria-label="Import repository"]')).not.toBeNull();
    await click("Create new project");
    const form = host.querySelector('form[aria-label="Create project"]')!;
    expect(host.querySelectorAll("form")).toHaveLength(1);
    expect(form.compareDocumentPosition(host.querySelector(".project-list")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(invoke.mock.calls.map(([, args]) => args?.method).filter(Boolean)).toEqual(["projects.list"]);
  });

  it("opens verified bootstrap-only state using the exact inspection without migration", async () => {
    setup(undefined, async () => ({ ...summary, storage: "ABSENT", schema_version: null, active_project: null }));
    await render();
    await inspectPath();
    expect(invoke.mock.calls).toContainEqual(["workspace_request", { method: "project.open_checked", params: {
      root: legacy.root, inspection_digest: "e".repeat(64), migrate: false,
    } }]);
    expect(host.querySelector(".project-result")?.textContent).toContain("No database");
    expect(host.querySelector(".onboarding-review")).toBeNull();
    expect(mutations().map(([, args]) => args.method)).toEqual(["project.open_checked"]);
  });

  it("requires explicit migration and discards the reviewed action when its path is edited", async () => {
    setup(async () => ({ ...currentInspection(legacy.root), status: "MIGRATION_REQUIRED", reason: "Verified supported older schema." }));
    await render();
    await inspectPath();
    expect(host.textContent).toContain("Verified supported older schema.");
    expect(mutations()).toHaveLength(0);
    await field("#project-root", "C:\\changed");
    expect(host.querySelector(".onboarding-review")).toBeNull();
    await field("#project-root", legacy.root);
    await click("Open project");
    await click("Migrate and open");
    expect(mutations()).toEqual([["workspace_request", { method: "project.open_checked", params: {
      root: legacy.root, inspection_digest: "e".repeat(64), migrate: true,
    } }]]);
  });

  it("requires fresh inspection after rejected migration and exposes no native diagnostic", async () => {
    setup(async () => ({ ...currentInspection(legacy.root), status: "MIGRATION_REQUIRED", reason: "Older schema." }),
      async () => { throw new Error("private identity detail"); });
    await render();
    await inspectPath();
    await click("Migrate and open");
    expect(host.textContent).toContain("Inspect the directory again before retrying");
    expect(host.textContent).not.toContain("private identity detail");
    expect(host.querySelector(".onboarding-review")).toBeNull();
  });

  it("carries an unmanaged directory into Create while requiring name entry and explicit creation", async () => {
    setup(async () => ({ ...currentInspection(legacy.root), status: "UNMANAGED", reason: "No private state.", inspection_digest: null }));
    await render();
    await inspectPath();
    expect(host.textContent).toContain("not a supported local Git repository");
    await click("Create project here");
    expect(host.querySelector<HTMLInputElement>("#project-root")?.value).toBe(legacy.root);
    expect(host.querySelector<HTMLInputElement>("#project-name")?.value).toBe("");
    expect(mutations()).toHaveLength(0);
    await click("Create project");
    expect(host.textContent).toContain("Enter a project name");
    await click("Cancel creation");
    expect(mutations()).toHaveLength(0);
  });

  it.each(["UNMANAGED", "INCOMPLETE_IMPORT"] as const)("routes %s to explicit import review without discovering automatically", async (status) => {
    setup(async () => ({ ...currentInspection(legacy.root), status, reason: "Import review required.", can_import: true }));
    await render();
    await inspectPath();
    const review = host.querySelector(".onboarding-review")!;
    const action = review.querySelector<HTMLButtonElement>(status === "UNMANAGED" ? "button:last-of-type" : "button")!;
    await act(async () => action.click());
    expect(host.querySelector<HTMLInputElement>("#import-root")?.value).toBe(legacy.root);
    expect(host.querySelector(".import-preview")).toBeNull();
    expect(invoke.mock.calls.map(([, args]) => args?.method)).not.toContain("import.inspect");
    expect(mutations()).toHaveLength(0);
  });

  it.each(["INVALID", "UNVERIFIED", "MISSING", "UNAVAILABLE"] as const)("presents the bounded %s decision without opening or offering mutation", async (status) => {
    setup(async () => ({ ...currentInspection(legacy.root), status, reason: `Bounded ${status} explanation.`, inspection_digest: null }));
    await render();
    await inspectPath();
    expect(host.querySelector(".onboarding-review")?.textContent).toContain(`Bounded ${status} explanation.`);
    expect(host.querySelectorAll(".onboarding-review button")).toHaveLength(0);
    await click("Cancel");
    expect(host.querySelector(".onboarding-review")).toBeNull();
    expect(mutations()).toHaveLength(0);
  });

  it("discards a late classification after changing flow and does not dispatch its opening", async () => {
    let finish: (value: ProjectInspection) => void = () => {};
    setup(() => new Promise((resolve) => { finish = resolve; }));
    await render();
    await inspectPath();
    await click("Cancel");
    await click("Create new project");
    await act(async () => finish(currentInspection(legacy.root) as ProjectInspection));
    expect(host.querySelector('form[aria-label="Create project"]')).not.toBeNull();
    expect(host.querySelector(".project-result")).toBeNull();
    expect(host.querySelector(".onboarding-review")).toBeNull();
    expect(mutations()).toHaveLength(0);
  });

  it("rejects duplicate forced Open submissions before a pending inspection returns", async () => {
    let finish: (value: ProjectInspection) => void = () => {};
    setup(() => new Promise((resolve) => { finish = resolve; }));
    await render();
    await click("Open existing project");
    await openManualPath("#project-root");
    await field("#project-root", legacy.root);
    await act(async () => {
      const form = host.querySelector('form[aria-label="Open project"]')!;
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    expect(invoke.mock.calls.filter(([, args]) => args?.method === "project.inspect")).toHaveLength(1);
    await act(async () => finish(currentInspection(legacy.root) as ProjectInspection));
    expect(mutations()).toHaveLength(1);
  });

  it("refreshes recents after a sent opening finishes without replacing later navigation", async () => {
    let finish: (value: ProjectSummary) => void = () => {};
    setup(undefined, () => new Promise((resolve) => { finish = resolve; }));
    await render();
    await inspectPath();
    await click("Home");
    await act(async () => finish(summary));
    expect(host.querySelector('[aria-current="page"]')?.textContent).toBe("Home");
    expect(host.querySelector(".project-result")).toBeNull();
    expect(invoke.mock.calls.filter(([, args]) => args?.method === "projects.list")).toHaveLength(2);
  });

  it("fails closed when an older backend only advertises the automatic legacy Open method", async () => {
    invoke.mockImplementation(async (command) => command === "backend_snapshot"
      ? { ...ready, handshake: { ...ready.handshake, methods: ["projects.list", "project.summary", "project.open"] } }
      : { projects: [legacy], next_offset: null });
    await render();
    expect(host.textContent).toContain("Safe project opening is unavailable");
    await click("Open existing project");
    await click("Open Project");
    expect(host.querySelector('form[aria-label="Open project"]')).toBeNull();
    expect(mutations()).toHaveLength(0);
  });
});

describe("desktop named project creation", () => {
  const created = {
    ...summary, root: "C:\\Projects\\Novo projeto", display_name: "Investigação clínica",
    active_project: null, active_workflow: null, storage: "ABSENT" as const, schema_version: null,
  };
  const entry: ProjectEntry = { ...legacy, ...created, availability: "AVAILABLE" };
  const card: CommandProjectCard = {
    ...entry, project_id: null, error: null, active_project: null, active_workflow: null,
    import_status: null, external_import: null, work_counts: null, focus_work_items: [],
    sessions: null, validation: { state: "NOT_VERIFIED", latest_run: null, outcome: null, active_plan_refs: [] },
    latest_context: null, lifecycle: null, attention: [],
  };

  function setup(create: () => Promise<ProjectSummary> = async () => created) {
    let saved = false;
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot" || command === "restart_backend") return {
        ...ready, handshake: { ...ready.handshake, methods: [...methods, "project.create", "command.overview", "command.project"] },
      };
      switch (payload.method) {
        case "project.inspect": return currentInspection(payload.params.root);
        case "projects.list": return { projects: saved ? [entry] : [], next_offset: null };
        case "project.create": {
          const result = await create();
          saved = true;
          return result;
        }
        case "project.open_checked":
        case "project.summary": return created;
        case "command.overview": return { projects: saved ? [card] : [], next_offset: null };
        case "command.project": return card;
        default: throw new Error("Unexpected method");
      }
    });
  }

  const creations = () => invoke.mock.calls.filter(([, payload]) => payload?.method === "project.create");

  async function fillCreation() {
    await click("Create new project");
    await field("#project-name", created.display_name);
    await openManualPath("#project-root");
    await field("#project-root", created.root);
  }

  it("validates fields and cancels before submitting without mutating or probing", async () => {
    setup();
    await render();
    await click("Create new project");
    await click("Create project");
    expect(host.querySelector("#project-name")?.getAttribute("aria-invalid")).toBe("true");
    expect(host.textContent).toContain("Enter a project name.");
    await field("#project-name", created.display_name);
    await openManualPath("#project-root");
    await field("#project-root", "relative/project");
    await click("Create project");
    expect(host.textContent).toContain("Enter an absolute local directory path.");
    await click("Cancel creation");
    expect(host.querySelector("#project-name")).toBeNull();
    expect(creations()).toHaveLength(0);
    expect(invoke.mock.calls.map(([, payload]) => payload?.method).filter(Boolean)).toEqual(["projects.list"]);
  });

  it("opens the returned summary and refreshes Home, Projects and Command using the saved name", async () => {
    setup();
    await render();
    await fillCreation();
    await click("Create project");
    expect(creations()).toEqual([["workspace_request", {
      method: "project.create", params: { root: created.root, display_name: created.display_name },
    }]]);
    expect(host.querySelector(".project-result h3")?.textContent).toBe(created.display_name);
    expect(host.querySelector('[aria-current="page"]')?.textContent).toBe("Projects");
    expect(invoke.mock.calls.some(([, payload]) => payload?.method === "project.open")).toBe(false);
    expect(host.textContent).not.toContain("GUI authoring is unavailable");
    await click("Continue with Project Manager");
    expect(host.textContent).toContain("GUI authoring is unavailable");
    expect(host.textContent).toContain("corinthos project --root . start");
    await click("Home");
    expect(host.querySelector(".project-list h3")?.textContent).toBe(created.display_name);
    await click("Command");
    expect(host.querySelector(".command-card h3")?.textContent).toBe(created.display_name);
    expect(invoke.mock.calls.some(([, payload]) => /^(providers\.|session\.start|flow\.start|review\.defer)/.test(payload?.method ?? ""))).toBe(false);
    await act(async () => root.render(null));
    await render();
    expect(host.querySelector(".project-list h3")?.textContent).toBe(created.display_name);
    await click(`Open ${created.display_name}`);
    expect(host.querySelector(".project-result h3")?.textContent).toBe(created.display_name);
    expect(creations()).toHaveLength(1);
  });

  it("counts supported Unicode code points and rejects unsupported name characters before dispatch", async () => {
    setup();
    await render();
    await fillCreation();
    await field("#project-name", "Hidden\u202ename");
    await click("Create project");
    expect(host.textContent).toContain("without control or unsupported characters");
    await field("#project-name", "🏛".repeat(129));
    await click("Create project");
    expect(creations()).toHaveLength(0);
    await field("#project-name", "🏛".repeat(128));
    await click("Create project");
    expect(creations()).toHaveLength(1);
    expect(creations()[0][1].params.display_name).toBe("🏛".repeat(128));
  });

  it("blocks duplicate submission and keeps a late success from replacing navigation", async () => {
    let finish: (value: ProjectSummary) => void = () => {};
    setup(() => new Promise((resolve) => { finish = resolve; }));
    await render();
    await fillCreation();
    await click("Create project");
    expect(host.querySelector<HTMLInputElement>("#project-name")?.disabled).toBe(true);
    await click("Create project");
    expect(creations()).toHaveLength(1);
    await click("Command");
    await act(async () => finish(created));
    expect(host.querySelector('[aria-current="page"]')?.textContent).toBe("Command");
    expect(host.querySelector(".project-result")).toBeNull();
    expect(host.querySelector(".command-card h3")?.textContent).toBe(created.display_name);
    await click("Home");
    expect(host.textContent).toContain(`Created ${created.display_name}`);
  });

  it("retains inputs and requires an explicit retry after a lost response", async () => {
    let attempts = 0;
    setup(async () => {
      attempts += 1;
      if (attempts === 1) throw new Error("private transport details");
      return created;
    });
    await render();
    await fillCreation();
    await click("Create project");
    expect(host.textContent).toContain("Creation was not confirmed");
    expect(host.textContent).not.toContain("private transport details");
    expect(host.querySelector<HTMLInputElement>("#project-name")?.value).toBe(created.display_name);
    expect(host.querySelector<HTMLInputElement>("#project-root")?.value).toBe(created.root);
    expect(creations()).toHaveLength(1);
    await click("Retry creation");
    expect(creations()).toHaveLength(2);
    expect(creations()[1]).toEqual(creations()[0]);
    expect(host.querySelector(".project-result h3")?.textContent).toBe(created.display_name);
  });

  it.each([
    ["INVALID_INPUT", "Check the project name and absolute local directory path"],
    ["CONFLICT", "different creation name or conflicting state"],
    ["ACCESS_DENIED", "Check its permissions"],
    ["STATE_UNAVAILABLE", "Its contents were retained"],
    ["REGISTRY_INCOMPLETE", "Private initialization completed"],
  ])("offers actionable %s recovery without losing the draft", async (code, message) => {
    setup(async () => { throw `PROJECT_CREATE_${code}`; });
    await render();
    await fillCreation();
    await click("Create project");
    expect(host.textContent).toContain(message);
    expect(host.querySelector<HTMLInputElement>("#project-name")?.value).toBe(created.display_name);
    expect(host.querySelector<HTMLInputElement>("#project-root")?.value).toBe(created.root);
    expect(creations()).toHaveLength(1);
    expect(host.textContent).toContain("Retry creation");
    expect(host.querySelector(".project-result")).toBeNull();
  });

  it("preserves an uncertain creation across reconnect and ignores the old response", async () => {
    let finish: (value: ProjectSummary) => void = () => {};
    let attempts = 0;
    setup(() => ++attempts === 1 ? new Promise((resolve) => { finish = resolve; }) : Promise.resolve(created));
    await render();
    await fillCreation();
    await click("Create project");
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    expect(host.textContent).toContain("Creation may have completed before disconnect");
    expect(host.textContent).toContain(created.root);
    await act(async () => finish(created));
    expect(host.querySelector(".project-result")).toBeNull();
    await click("Reconnect");
    expect(host.querySelector<HTMLInputElement>("#project-name")?.value).toBe(created.display_name);
    expect(host.querySelector<HTMLInputElement>("#project-root")?.value).toBe(created.root);
    expect(creations()).toHaveLength(1);
    await click("Retry creation");
    expect(creations()).toHaveLength(2);
    expect(host.querySelector(".project-result h3")?.textContent).toBe(created.display_name);
  });

  it("leaves an in-flight creation inspectable without claiming backend cancellation", async () => {
    let finish: (value: ProjectSummary) => void = () => {};
    setup(() => new Promise((resolve) => { finish = resolve; }));
    await render();
    await fillCreation();
    await click("Create project");
    await click("Leave creation");
    expect(host.textContent).toContain("does not cancel backend initialization");
    await act(async () => finish(created));
    expect(host.querySelector('[aria-current="page"]')?.textContent).toBe("Home");
    expect(host.querySelector(".project-result")).toBeNull();
    expect(host.textContent).toContain(`Created ${created.display_name}`);
    expect(creations()).toHaveLength(1);
  });
});

describe("desktop external repository import", () => {
  const digest = "a".repeat(64);
  const snapshot = "b".repeat(64);
  const repository = { ...legacy, root: "C:\\external", display_name: "external", schema_version: 9, storage: "CURRENT" as const };
  const discovery: ImportDiscovery = {
    root: repository.root, label: "DISCOVERED", object_format: "sha256", head: "c".repeat(64), branch: "main",
    dirty: true, commit_allowed: true, existing_state: false, tracked_count: 5,
    languages: { Python: 2 }, manifests: [{ path: "pyproject.toml", ecosystem: "python" }],
    docs: ["<b>README.md</b>"], tests: ["tests/test_payments.py"],
    candidates: [{ path: "src", ecosystem: "python" }], exclusions: ["SENSITIVE_PATH"],
    ambiguities: [{
      code: "DIRTY_TRACKED", explanation: "Tracked content differs from HEAD.", required: true, options: ["CONFIRM"],
    }],
    discovery_digest: digest,
  };

  function setupImport(inspect = async () => discovery,
    commit: () => Promise<ImportResult> = async () => ({ root: discovery.root, status: "REJECTED", error: "REPOSITORY_CHANGED" })) {
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot" || command === "restart_backend") return { ...ready, handshake: {
        ...ready.handshake, methods: [...methods, "project.create", "import.inspect", "import.commit", "import.status"],
      } };
      switch (payload.method) {
        case "projects.list": return { projects: [], next_offset: null };
        case "import.inspect": return inspect();
        case "import.commit": return commit();
        case "import.status": return { root: discovery.root, status: "INCOMPLETE", error: "INCOMPLETE" };
        case "project.inspect": return currentInspection(discovery.root);
        case "project.open_checked": return { ...summary, root: discovery.root };
        default: throw new Error("Unexpected method");
      }
    });
  }

  async function reviewImport() {
    await click("Import Git repository");
    await openManualPath("#import-root");
    await field("#import-root", discovery.root);
    await click("Inspect repository");
  }

  it("discards a late import discovery after choosing another flow", async () => {
    let finish: (value: ImportDiscovery) => void = () => {};
    setupImport(() => new Promise((resolve) => { finish = resolve; }));
    await render();
    await reviewImport();
    await click("Cancel import");
    await click("Create new project");
    await act(async () => finish(discovery));
    expect(host.querySelector('form[aria-label="Create project"]')).not.toBeNull();
    expect(host.querySelector(".import-preview")).toBeNull();
    expect(invoke.mock.calls.map(([, args]) => args?.method)).not.toContain("import.commit");
  });

  it("invalidates a changed repository rejection and requires fresh digest review", async () => {
    setupImport();
    await render();
    await reviewImport();
    await act(async () => host.querySelector<HTMLInputElement>('.import-preview input[type="checkbox"]')!.click());
    await click("Import repository");
    expect(host.textContent).toContain("repository changed after inspection");
    expect(host.querySelector(".import-preview")).toBeNull();
    expect(invoke.mock.calls.filter(([, args]) => args?.method === "import.commit")).toHaveLength(1);
    expect(invoke.mock.calls.map(([, args]) => args?.method)).not.toContain("project.open_checked");
  });

  it("keeps a confirmed import recoverable when registration fails and retries only checked opening", async () => {
    setupImport(undefined, async () => ({ root: discovery.root, status: "IMPORTED", label: "CONFIRMED", project_id: "project-1",
      discovery_digest: digest, snapshot_digest: snapshot, error: "REGISTRY_UNAVAILABLE" }));
    await render();
    await reviewImport();
    await act(async () => host.querySelector<HTMLInputElement>('.import-preview input[type="checkbox"]')!.click());
    await click("Import repository");
    expect(host.textContent).toContain("Projects could not save the entry");
    expect(host.querySelector(".import-confirmed")?.textContent).toContain(snapshot);
    expect(invoke.mock.calls.map(([, args]) => args?.method)).not.toContain("project.open_checked");
    await click("Retry opening imported project");
    expect(host.querySelector(".project-result code")?.textContent).toBe(discovery.root);
    expect(invoke.mock.calls.filter(([, args]) => args?.method === "import.commit")).toHaveLength(1);
    expect(invoke.mock.calls).toContainEqual(["workspace_request", { method: "project.open_checked", params: {
      root: discovery.root, inspection_digest: "e".repeat(64), migrate: false,
    } }]);
  });

  it("retains an uncertain import and its directory across disconnect while blocking abandonment and duplicates", async () => {
    let finish: (value: ImportResult) => void = () => {};
    let recovering = false;
    setupImport(async () => ({ ...discovery, existing_state: recovering }),
      () => new Promise((resolve) => { finish = resolve; }));
    await render();
    await reviewImport();
    await act(async () => host.querySelector<HTMLInputElement>('.import-preview input[type="checkbox"]')!.click());
    await click("Import repository");
    await click("Create new project");
    await click("Cancel import");
    await click("Command");
    await click("Import repository");
    await act(async () => host.querySelector('form[aria-label="Import repository"]')!.dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true })));
    expect(host.querySelector('form[aria-label="Import repository"]')).not.toBeNull();
    expect(invoke.mock.calls.filter(([, args]) => args?.method === "import.commit")).toHaveLength(1);
    expect(invoke.mock.calls.filter(([, args]) => args?.method === "import.inspect")).toHaveLength(1);
    await act(async () => lifecycle({ payload: { method: "backend.disconnected" } }));
    await act(async () => finish({ root: discovery.root, status: "REJECTED", error: "INCOMPLETE" }));
    expect(host.textContent).toContain("Disconnected");
    recovering = true;
    await click("Reconnect");
    expect(host.textContent).toContain("Import was not confirmed");
    expect(host.querySelector<HTMLInputElement>("#import-root")?.value).toBe(discovery.root);
    await click("Inspect repository");
    expect(host.textContent).toContain("previous import is incomplete");
    expect(invoke.mock.calls.map(([, args]) => args?.method)).not.toContain("project.open_checked");
  });

  it("keeps inspection passive and imports only after explicit ambiguity confirmation", async () => {
    let imported = false;
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot") return { ...ready, handshake: {
        ...ready.handshake, methods: [...methods, "import.inspect", "import.commit"],
      } };
      switch (payload.method) {
        case "project.inspect": return currentInspection(payload.params.root);
        case "projects.list": return { projects: imported ? [repository] : [], next_offset: null };
        case "import.inspect": return discovery;
        case "import.commit":
          imported = true;
          return { root: discovery.root, status: "IMPORTED", label: "CONFIRMED", project_id: "project-1",
            discovery_digest: digest, snapshot_digest: snapshot, error: null };
        case "project.open_checked": return { ...summary, root: discovery.root, schema_version: 9, display_name: "external",
          active_project: null, active_workflow: null };
        case "work.tree": return { nodes: [], next_offset: null };
        default: throw new Error("Unexpected method");
      }
    });
    await render();
    const panel = () => host.querySelector(".repository-import");
    expect(invoke.mock.calls.map(([_command, args]) => args?.method)).not.toContain("import.inspect");
    await click("Import Git repository");
    await openManualPath("#import-root");
    await field("#import-root", "C:\\external\\");
    await click("Inspect repository");
    expect(invoke.mock.calls).toEqual([
      ["backend_snapshot"],
      ["workspace_request", { method: "projects.list", params: { offset: 0 } }],
      ["workspace_request", { method: "import.inspect", params: { root: "C:\\external\\" } }],
    ]);
    expect(panel()?.textContent).toContain("DISCOVERED repository facts");
    expect(panel()?.textContent).toContain("Git object formatSHA-256");
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
    expect(host.querySelector(".import-confirmed")?.textContent).toContain(snapshot);
    expect(host.querySelector(".project-list")?.textContent).toContain("Open external");
    expect(host.querySelector(".project-result")?.textContent).not.toContain("Project Contract");
    await click("Work");
    expect(host.querySelector(".work-form")?.textContent).toContain("Create a Work Item");
  });
});

describe("native folder selection across project flows", () => {
  const selected = "C:\\Projects\\Olá world";
  let pick: () => Promise<string | null>;
  let settle: (() => void) | undefined;
  const discovery: ImportDiscovery = {
    root: selected, label: "DISCOVERED", object_format: "sha256", head: "c".repeat(64), branch: "main",
    dirty: true, commit_allowed: true, existing_state: false, tracked_count: 1,
    languages: {}, manifests: [], docs: [], tests: [], candidates: [], exclusions: [],
    ambiguities: [{ code: "DIRTY_TRACKED", explanation: "Review changes.", required: true, options: [] }],
    discovery_digest: "d".repeat(64),
  };

  beforeEach(() => {
    settle = undefined;
    pick = async () => selected;
    invoke.mockImplementation(async (command, payload) => {
      if (command === "select_project_directory") return pick();
      if (command === "backend_snapshot") return { ...ready, handshake: {
        ...ready.handshake, methods: [...methods, "project.create", "import.inspect", "import.commit"],
      } };
      switch (payload?.method) {
        case "project.inspect": return currentInspection(payload.params.root);
        case "projects.list": return { projects: [], next_offset: null };
        case "project.create":
        case "project.open_checked": return { ...summary, root: payload.params.root,
          display_name: payload.params.display_name ?? "Selected project", work_items: null };
        case "import.inspect": return { ...discovery, root: payload.params.root };
        default: throw new Error("Unexpected method");
      }
    });
  });

  afterEach(async () => { await act(async () => settle?.()); });

  async function select(importing = false) {
    const label = importing ? "Select folder for local repository path" : "Select folder for project root";
    await act(async () => host.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`)!.click());
  }

  const operations = () => invoke.mock.calls.filter(([command]) => command === "workspace_request")
    .map(([, args]) => args.method);

  it.each(["Open", "Create", "Import"])("fills the %s draft and waits for its explicit action", async (flow) => {
    await render();
    await click(flow === "Import" ? "Import Git repository" : flow === "Create" ? "Create new project" : "Open existing project");
    if (flow === "Create") {
      await field("#project-name", "Chosen name");
    }
    await select(flow === "Import");
    expect(host.querySelector<HTMLInputElement>(flow === "Import" ? "#import-root" : "#project-root")!.value).toBe(selected);
    expect(host.textContent).toContain(selected);
    expect(operations()).toEqual(["projects.list"]);
    await click(flow === "Import" ? "Inspect repository" : flow === "Create" ? "Create project" : "Open project");
    const method = flow === "Import" ? "import.inspect" : flow === "Create" ? "project.create" : "project.open_checked";
    expect(invoke.mock.calls).toContainEqual(["workspace_request", { method,
      params: flow === "Create" ? { root: selected, display_name: "Chosen name" } : flow === "Open"
        ? { root: selected, inspection_digest: "e".repeat(64), migrate: false } : { root: selected },
    }]);
  });

  it("preserves import review on cancel and invalidates it when a different folder is selected", async () => {
    await render();
    await click("Import Git repository");
    await select(true);
    await click("Inspect repository");
    await act(async () => host.querySelector<HTMLInputElement>('.import-preview input[type="checkbox"]')!.click());
    const checkbox = () => host.querySelector<HTMLInputElement>('.import-preview input[type="checkbox"]');
    expect(checkbox()!.checked).toBe(true);
    pick = async () => null;
    await select(true);
    expect(checkbox()!.checked).toBe(true);
    expect(host.querySelector<HTMLInputElement>("#import-root")!.value).toBe(selected);
    pick = async () => "C:\\different";
    await select(true);
    expect(host.querySelector(".import-preview")).toBeNull();
    expect(host.querySelector<HTMLInputElement>("#import-root")!.value).toBe("C:\\different");
    expect(operations()).toEqual(["projects.list", "import.inspect"]);
  });

  it("requires manual disclosure activation and rejects disabled path entry without dispatching", async () => {
    await render();
    await click("Open existing project");
    const target = host.querySelector<HTMLInputElement>("#project-root")!;
    const input = vi.fn();
    target.addEventListener("input", input);
    expect(target.closest("details")!.open).toBe(false);
    await expect(field("#project-root", "C:\\hidden")).rejects.toThrow("Input unavailable");
    expect(target.value).toBe("");
    expect(input).not.toHaveBeenCalled();
    await openManualPath("#project-root");
    await field("#project-root", "C:\\manual");
    expect(target.value).toBe("C:\\manual");
    expect(input).toHaveBeenCalledOnce();
    let finish: (value: string | null) => void = () => {};
    pick = () => new Promise((resolve) => { finish = resolve; settle = () => resolve(null); });
    await select();
    expect(target.disabled).toBe(true);
    await expect(field("#project-root", "C:\\disabled")).rejects.toThrow("Input unavailable");
    expect(target.value).toBe("C:\\manual");
    expect(input).toHaveBeenCalledOnce();
    expect(operations()).toEqual(["projects.list"]);
    await act(async () => finish(null));
  });

  it("disables submissions and cross-flow picking while a native dialog is outstanding", async () => {
    let finish: (value: string | null) => void = () => {};
    pick = () => new Promise((resolve) => { finish = resolve; settle = () => resolve(null); });
    await render();
    await click("Open existing project");
    await openManualPath("#project-root");
    await field("#project-root", "C:\\original");
    await select();
    await click("Import Git repository");
    expect(host.querySelector("#import-root")).toBeNull();
    expect(host.querySelector<HTMLButtonElement>('[aria-label="Select folder for project root"]')!.disabled).toBe(true);
    await act(async () => {
      host.querySelectorAll("form").forEach((form) => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    });
    expect(invoke.mock.calls.filter(([command]) => command === "select_project_directory")).toHaveLength(1);
    expect(operations()).toEqual(["projects.list"]);
    await act(async () => finish(null));
    expect(host.querySelector<HTMLInputElement>("#project-root")!.value).toBe("C:\\original");
  });

  it("ignores folder selection after navigating away and returning", async () => {
    let finish: (value: string | null) => void = () => {};
    pick = () => new Promise((resolve) => { finish = resolve; settle = () => resolve(null); });
    await render();
    await click("Open existing project");
    await select();
    await click("Command");
    await act(async () => finish(selected));
    await click("Home");
    expect(host.querySelector<HTMLInputElement>("#project-root")!.value).toBe("");
    expect(operations()).toEqual(["projects.list"]);
  });
});

describe("desktop Command and project navigation", () => {
  const secondRoot = "C:\\second";
  const projection: ContextProjection = {
    projection_id: "projection-exact", project_id: "project", work_item_id: item.work_item_id,
    target_provider: "codex", selector_policy_version: 1, redaction_policy_version: 1,
    projection_digest: "e".repeat(64), source_set_digest: "f".repeat(64), created_at: item.updated_at,
  };
  const session: AgentSession = {
    session_id: "session-exact", project_id: "project", work_item_id: item.work_item_id,
    provider: "codex", provider_version: "1.2", model: null, state: "RUNNING", run_generation: 2,
    created_at: item.updated_at, updated_at: item.updated_at, started_at: item.updated_at, finished_at: null,
    input_tokens: 0, output_tokens: null, cached_tokens: null, duration_ms: null, reported_cost: null,
    reported_cost_currency: null, timeout_seconds: 120, read_only: true, can_cancel: true,
    can_recover: false, cleanup_unconfirmed: false,
  };
  const focus = {
    project_id: "project", work_item_id: item.work_item_id, objective: item.objective,
    scope_level: item.scope_level, status: item.status, stage: "EXECUTING",
    blockers: [], next_actions: [], validation_state: "NOT_VERIFIED", validation_result_refs: [],
    latest_session: session, latest_projection: projection, provider_continuity: null,
  };
  const first: CommandProjectCard = {
    ...legacy, project_id: "project", storage: "CURRENT", schema_version: 9, error: null,
    active_project: null, active_workflow: null, import_status: null, external_import: null,
    work_counts: { total: 1, by_status: { OPEN: 1 }, by_scope: { CHANGE: 1 } },
    focus_work_items: [focus], sessions: { total: 1, active: { ...session, context_projection_id: projection.projection_id }, latest_terminal: null },
    validation: { state: "NOT_VERIFIED", latest_run: null, active_plan_refs: [], outcome: null },
    latest_context: { ...projection, provider: "codex" }, lifecycle: null, attention: [],
  };
  const second: CommandProjectCard = {
    ...first, root: secondRoot, display_name: "Second project", project_id: "second", sessions: null,
    latest_context: null, focus_work_items: [{ ...focus, project_id: "second", stage: "PLANNING_REQUIRED", latest_session: null, latest_projection: null }],
    validation: { ...first.validation, state: "VERIFIED" },
    attention: [{ kind: "PLANNING_REQUIRED", owner_type: "WORK_ITEM", owner_id: item.work_item_id,
      reason_code: "PLANNING_REQUIRED", severity: "ACTION", summary: "Plan the second Work Item.", navigation: "WORK", action_ids: [] }],
  };
  const unavailable: CommandProjectCard = {
    ...first, ...missing, project_id: null, error: "PROJECT_MISSING", active_project: null,
    focus_work_items: [], sessions: null, latest_context: null,
  };
  const commandMethods = [
    ...methods, "command.overview", "command.project", "flow.status", "session.list", "session.get",
    "session.events", "switch.status", "switch.execute", "context.list", "context.get", "context.for_session", "context.sources",
  ];
  const review: ReviewDetails = {
    review: { kind: "WORKFLOW", session_id: "planning-exact", project_id: "second", version: 7,
      state: "AWAITING_EXPLICIT_APPROVAL", digest: "a".repeat(64), expires_at: null },
    presentation: "Exact <approved document> version and diff.",
    approve_command: "corinthos workflow publish planning-exact",
    can_defer: true, redactions: [],
  };

  function setup(extra: string[] = [], cards = [first, second, unavailable]) {
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot") {
        const [codex] = ready.providers.providers;
        return { ...ready, handshake: { ...ready.handshake, methods: [...commandMethods, ...extra] },
          providers: { providers: [codex, { ...codex, provider: "claude" }] } };
      }
      const { method, params } = payload;
      switch (method) {
        case "projects.list": return { projects: [legacy], next_offset: null };
        case "command.overview": return { projects: params.offset ? [second] : cards, next_offset: params.offset ? null : 10 };
        case "command.project": return params.root === secondRoot ? second : first;
        case "project.summary": return { ...summary, root: params.root, schema_version: 9 };
        case "work.tree": return { nodes: [{ ...item, depth: 0 }], next_offset: null };
        case "work.get": return { ...item, acceptance_criteria: ["A concrete criterion."] };
        case "flow.status": return {
          ...focus, work_item_id: params.work_item_id, selected_scope: "CHANGE", recommended_scope: "TASK",
          minimum_required_scope: "TASK", flow_kind: "CHANGE", policy_version: 1, signals: [], unknowns: [],
          gates: [], validation_policy: null, workflow_ref: null, execution: null, latest_handoff: null,
          report_status: "NOT_VERIFIED", can_start: false, can_complete: false,
        };
        case "session.list": return { sessions: [session], next_offset: null };
        case "session.get": return session;
        case "session.events": return { events: [], next_after_sequence: 0 };
        case "context.for_session": return { projection };
        case "context.list": return { projections: [projection], next_offset: null };
        case "context.get": return projection;
        case "context.sources": return { sources: [], next_offset: null };
        case "switch.status": return {
          switch_key: "a".repeat(64), project_id: "project", work_item_id: item.work_item_id,
          source_session_id: session.session_id, source_provider: "codex", source_generation: 2,
          source_state: "RUNNING", cleanup_unconfirmed: false, target_provider: "claude",
          stage: "SOURCE_ACTIVE", handoff_id: null, handoff_digest: null, projection_id: null,
          projection_digest: null, target_session_id: null, target_state: null, blocker: null,
        };
        case "review.list": return { reviews: [], next_offset: null };
        case "review.get": return review;
        case "review.defer": return { ...review.review, state: "READY_FOR_REVIEW", version: 8 };
        default: throw new Error("Unexpected method");
      }
    });
  }

  function card(rootPath: string) {
    return Array.from(host.querySelectorAll(".command-card")).find((entry) =>
      entry.querySelector(".command-card-root")?.textContent === rootPath)!;
  }

  async function clickWithin(container: Element, label: string) {
    const button = Array.from(container.querySelectorAll("button")).find((entry) => entry.textContent === label);
    if (!button) throw new Error(`Button missing: ${label}`);
    await act(async () => button.click());
  }

  it("opens exact Work, Session and Projection from Command and preserves selected Work across tabs", async () => {
    setup();
    await render();
    await click("Command");
    expect(card(first.root)?.textContent).toContain("Active sessionsession-exact");
    expect(card(secondRoot)?.textContent).toContain("PLANNING_REQUIRED");
    expect(card(missing.root)?.querySelector('[role="alert"]')?.textContent).toContain("PROJECT_MISSING");
    await clickWithin(card(first.root), "Open Work Item");
    expect(host.querySelector(".work-selection")?.textContent).toContain(`Work Item ID${item.work_item_id}`);
    expect(host.querySelector(".work-selection")?.textContent).toContain("A concrete criterion.");
    await click("Open Agent Session");
    expect(host.querySelector(".session-detail h4")?.textContent).toBe(session.session_id);
    await click("Open Context Projection");
    expect(host.querySelector(".projection-detail")?.textContent).toContain(projection.projection_digest);
    await click("Work");
    expect(host.querySelector(".work-selection")?.textContent).toContain(item.work_item_id);
    expect(host.querySelector(".workspace-content .project-views")).not.toBeNull();
    expect(host.querySelector(".app-sidebar .project-views")).toBeNull();
    await click("Settings");
    await click("Projects");
    expect(host.querySelector(".project-result code")?.textContent).toBe(first.root);
    expect(host.querySelector(".work-selection")?.textContent).toContain(item.work_item_id);
    expect(invoke.mock.calls.map(([_command, args]) => args?.method)).not.toContain("project.open");
  });

  it("opens attention in the independent project and displays independent Validation state", async () => {
    setup();
    await render();
    await click("Command");
    await clickWithin(card(secondRoot), "Open Attention");
    expect(host.querySelector(".project-result code")?.textContent).toBe(secondRoot);
    expect(host.querySelector(".work-selection code")?.textContent).toBe(item.work_item_id);
    await click("Validation");
    expect(host.querySelector(".validation-view [data-status]")?.textContent).toBe("VERIFIED");
    await click("Command");
    await clickWithin(card(first.root), "Open Project");
    await click("Validation");
    expect(host.querySelector(".validation-view [data-status]")?.textContent).toBe("NOT_VERIFIED");
  });

  it("bounds overview paging, polls only while visible, and does not probe providers", async () => {
    vi.useFakeTimers();
    try {
      setup();
      await render();
      await click("Command");
      await click("More Command projects");
      expect(host.querySelectorAll(".command-card")).toHaveLength(1);
      expect(invoke.mock.calls).toContainEqual(["workspace_request", { method: "command.overview", params: { offset: 10 } }]);
      const reads = () => invoke.mock.calls.filter(([_command, args]) => args?.method === "command.overview").length;
      let last = reads();
      const hide = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
      await act(async () => vi.advanceTimersByTimeAsync(4000));
      expect(reads()).toBe(last);
      hide.mockReturnValue(false);
      await act(async () => vi.advanceTimersByTimeAsync(4000));
      expect(reads()).toBe(++last);
      await click("Refresh Command");
      expect(reads()).toBe(++last);
      await click("Home");
      await act(async () => vi.advanceTimersByTimeAsync(8000));
      expect(reads()).toBe(last);
      expect(invoke.mock.calls.map(([_command, args]) => args?.method)).not.toContain("providers.refresh");
      hide.mockRestore();
    } finally {
      vi.useRealTimers();
    }
  });

  it("routes the Command switch to an exact session and awaits an explicit target choice", async () => {
    setup();
    await render();
    await click("Command");
    await clickWithin(card(first.root), "Switch Provider");
    expect(host.querySelector(".session-detail h4")?.textContent).toBe(session.session_id);
    expect(host.querySelector<HTMLSelectElement>("#switch-target-provider")?.value).toBe("");
    const calls = () => invoke.mock.calls.map(([_command, args]) => args?.method);
    expect(calls()).not.toContain("switch.status");
    await field("#switch-target-provider", "claude");
    expect(calls()).toContain("switch.status");
    expect(calls()).not.toContain("switch.execute");
  });

  it("opens exact frozen review evidence and defers only with explicit confirmation and CAS", async () => {
    const withReview: CommandProjectCard = { ...second, attention: [{
      kind: "WORKFLOW_REVIEW_PENDING", owner_type: "WORKFLOW_PLANNING", owner_id: review.review.session_id,
      reason_code: "WORKFLOW_REVIEW_PENDING", severity: "ACTION", summary: "Review exact decision.", navigation: "OVERVIEW", action_ids: [],
    }] };
    setup(["review.list", "review.get", "review.defer"], [first, withReview]);
    await render();
    await click("Command");
    await clickWithin(card(secondRoot), "Open Attention");
    expect(host.querySelector(".review-presentation")?.textContent).toBe(review.presentation);
    expect(host.querySelector(".review-detail")?.textContent).toContain(review.approve_command);
    await click("Defer review");
    expect(invoke.mock.calls.map(([_command, args]) => args?.method)).not.toContain("review.defer");
    await act(async () => host.querySelector<HTMLInputElement>(".review-detail input")?.click());
    await click("Defer review");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", { method: "review.defer", params: {
      root: secondRoot, kind: "WORKFLOW", session_id: review.review.session_id,
      version: review.review.version, digest: review.review.digest,
    } }]);
    expect(host.textContent).toContain("Decision deferred.");
  });
});

describe("desktop provider settings", () => {
  it("uses cached status until explicit refresh, keeps configuration separate, and fences reset", async () => {
    const [codex] = ready.providers.providers;
    const observed = { ...codex, observation: "OBSERVED" };
    invoke.mockImplementation(async (command, payload) => {
      if (command === "backend_snapshot") return { ...ready, handshake: { ...ready.handshake,
        methods: [...methods, "settings.providers", "providers.refresh", "provider.configure", "provider.reset"],
      } };
      switch (payload.method) {
        case "project.inspect": return currentInspection(payload.params.root);
        case "projects.list": return { projects: [], next_offset: null };
        case "providers.refresh": return [observed];
        case "provider.configure": return [{ ...observed, configuration: "global", executable: payload.params.executable }];
        case "provider.reset": return [observed];
        default: throw new Error("Unexpected method");
      }
    });
    await render();
    await click("Settings");
    expect(host.textContent).toContain("Not yet observed");
    expect(invoke.mock.calls.map(([_command, args]) => args?.method)).not.toContain("providers.refresh");
    await click("Refresh provider observations");
    expect(host.textContent).toContain("Previously observed");
    await field("#settings-provider", "codex");
    await field("#settings-executable", "C:\\tools\\codex.exe");
    await click("Save provider");
    expect(invoke.mock.calls).toContainEqual(["workspace_request", { method: "provider.configure",
      params: { provider: "codex", executable: "C:\\tools\\codex.exe" },
    }]);
    await click("Reset provider");
    expect(invoke.mock.calls.map(([_command, args]) => args?.method)).not.toContain("provider.reset");
    await act(async () => host.querySelector<HTMLInputElement>(".provider-form input[type='checkbox']")?.click());
    await click("Reset provider");
    expect(invoke.mock.calls.filter(([_command, args]) => args?.method === "providers.refresh")).toHaveLength(1);
    expect(invoke.mock.calls).toContainEqual(["workspace_request", { method: "provider.reset", params: { provider: "codex" } }]);
  });

  it("announces missing Command methods without sending calls", async () => {
    invoke.mockResolvedValueOnce(ready).mockResolvedValueOnce({ projects: [], next_offset: null });
    await render();
    await click("Command");
    expect(host.textContent).toContain("Command is unavailable");
    expect(invoke.mock.calls).toHaveLength(2);
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
        case "project.inspect": return currentInspection(payload.params.root);
        case "projects.list": return { projects: [legacy], next_offset: null };
        case "project.open_checked":
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
    await click("Work");
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
        case "project.inspect": return currentInspection(payload.params.root);
        case "switch.status":
        case "switch.execute": return switchRequest?.(payload.method);
        case "projects.list": return { projects: [legacy], next_offset: null };
        case "project.open_checked": return { ...summary, schema_version: 8 };
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
        case "project.inspect": return currentInspection(payload.params.root);
        case "projects.list": return { projects: [legacy], next_offset: null };
        case "project.open_checked": return { ...summary, schema_version: 9 };
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
