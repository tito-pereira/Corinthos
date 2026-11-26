// SPDX-License-Identifier: MPL-2.0
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import { App } from "./App";

const { invoke, listen } = vi.hoisted(() => ({ invoke: vi.fn(), listen: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen }));

it("keeps shell and Update reachable without calling removed Engine methods", async () => {
  vi.resetAllMocks();
  const environment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean };
  environment.IS_REACT_ACT_ENVIRONMENT = true;
  listen.mockResolvedValue(() => {});
  invoke.mockImplementation(async (name, payload) => {
    if (name === "backend_snapshot") return {
      connection: "ready", desktop: { version: "0.1.0", identifier: "org.corinthos.desktop" },
      handshake: { protocol_version: 2, version: "0.1.0", methods: ["skill.run", "project.init", "update.status", "update.check"] },
      providers: { providers: [] },
    };
    if (name === "workspace_request" && payload.method === "update.status") return {
      operation_id: null, installed_version: "0.1.0", candidate_version: null,
      phase: "IDLE", progress: 0, message: "Ready", error: null, can_start: false, can_finish: false,
    };
    throw new Error("Unexpected old method: " + payload?.method);
  });
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  try {
    await act(async () => root.render(<App />));
    expect(host.textContent).toContain("Prototype workspace backend");
    expect(host.textContent).toContain("Engine screens are unavailable");
    expect(host.querySelector(".brand-logo")).not.toBeNull();
    expect(host.textContent).toContain("Corinthos updates");
    expect(invoke.mock.calls.filter(([name]) => name === "workspace_request").map(([, payload]) => payload.method)).toEqual(["update.status"]);
    const settings = Array.from(host.querySelectorAll("button")).find(button => button.textContent === "Settings")!;
    await act(async () => settings.click());
    expect(host.textContent).toContain("Prototype workspace backend");
  } finally {
    await act(async () => root.unmount());
    host.remove();
    environment.IS_REACT_ACT_ENVIRONMENT = false;
  }
});
