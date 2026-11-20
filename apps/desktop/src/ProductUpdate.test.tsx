// SPDX-License-Identifier: MPL-2.0
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProductUpdate } from "./ProductUpdate";
import type { ProductUpdateStatus } from "./bridge";

const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));

const methods = ["update.check", "update.start", "update.status", "update.finish"];
const operation = "a".repeat(32);
const idle: ProductUpdateStatus = {
  operation_id: null, installed_version: "0.1.0", candidate_version: null, phase: "IDLE",
  progress: 0, message: "Updates are installed only when requested.", error: null,
  can_start: false, can_finish: false,
};
const available: ProductUpdateStatus = {
  ...idle, operation_id: operation, candidate_version: "0.1.1", phase: "AVAILABLE",
  message: "Corinthos 0.1.1 is available.", can_start: true,
};
const staged: ProductUpdateStatus = {
  ...available, phase: "STAGING", message: "Verifying update files.", progress: 50, can_start: false,
};
const ready: ProductUpdateStatus = {
  ...staged, phase: "READY", message: "Ready to restart.", progress: 75, can_finish: true,
};
const environment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean };
let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  environment.IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.useRealTimers();
  environment.IS_REACT_ACT_ENVIRONMENT = false;
});

async function render(allowed = methods) {
  await act(async () => root.render(<ProductUpdate methods={allowed} />));
}

function button(label: string) {
  const found = Array.from(host.querySelectorAll("button")).find((node) => node.textContent?.trim() === label);
  if (!found) throw new Error(`Button missing: ${label}`);
  return found;
}

async function click(label: string) {
  await act(async () => button(label).click());
}

describe("explicit product updates", () => {
  it("reads status passively and starts only after explicit check and update actions", async () => {
    invoke.mockResolvedValueOnce(idle).mockResolvedValueOnce(available);
    await render();
    expect(invoke.mock.calls).toEqual([["workspace_request", { method: "update.status", params: {} }]]);
    expect(host.textContent).toContain("0.1.0");
    await click("Check for updates");
    expect(invoke).toHaveBeenLastCalledWith("workspace_request", { method: "update.check", params: {} });
    expect(host.textContent).toContain("0.1.1");
    let complete: (value: ProductUpdateStatus) => void = () => {};
    invoke.mockReturnValueOnce(new Promise<ProductUpdateStatus>((resolve) => { complete = resolve; }));
    await act(async () => { button("Update Corinthos").click(); button("Update Corinthos").click(); });
    expect(invoke.mock.calls.filter(([, args]) => args?.method === "update.start")).toHaveLength(1);
    expect(invoke).toHaveBeenLastCalledWith("workspace_request", {
      method: "update.start", params: { operation_id: operation },
    });
    await act(async () => complete(staged));
    expect(host.querySelector("progress")?.value).toBe(50);
  });

  it("polls the same operation and uses only the narrow native restart handoff", async () => {
    vi.useFakeTimers();
    invoke.mockResolvedValueOnce(staged).mockResolvedValueOnce(ready);
    await render();
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(invoke).toHaveBeenLastCalledWith("workspace_request", {
      method: "update.status", params: { operation_id: operation },
    });
    expect(host.textContent).toContain("Restarting closes this window");
    invoke.mockResolvedValueOnce({ ...ready, phase: "BUSY", error: {
      code: "BUSY", message: "Close the other Corinthos command, then retry.",
    } });
    await click("Restart to finish");
    expect(invoke).toHaveBeenLastCalledWith("finish_update", { operationId: operation });
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("Close the other Corinthos command");
    const calls = invoke.mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(invoke.mock.calls).toHaveLength(calls);
  });

  it("restores an interrupted operation from status without checking or installing automatically", async () => {
    invoke.mockResolvedValueOnce({ ...ready, phase: "RECOVERY_REQUIRED", message: "Recover the interrupted installation." });
    await render();
    expect(button("Recover and restart").disabled).toBe(false);
    expect(invoke.mock.calls).toEqual([["workspace_request", { method: "update.status", params: {} }]]);
    invoke.mockResolvedValueOnce({ ...ready, phase: "WAITING_FOR_EXIT", message: "Waiting for this window to close." });
    await click("Recover and restart");
    expect(invoke).toHaveBeenLastCalledWith("finish_update", { operationId: operation });
    expect(host.textContent).toContain("Waiting for this window to close.");
  });

  it("requires a status read after an uncertain request and tolerates an older backend", async () => {
    invoke.mockRejectedValueOnce(new Error("Disconnected"));
    await render();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("Read the status before retrying");
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(button("Check for updates").disabled).toBe(true);
    invoke.mockResolvedValueOnce({ ...idle, phase: "NO_UPDATE", message: "No update is available." });
    await click("Read update status");
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(host.textContent).toContain("None available");
    await render([]);
    expect(host.textContent).toContain("unavailable in this backend");
    expect(invoke).toHaveBeenCalledTimes(2);
  });
});
