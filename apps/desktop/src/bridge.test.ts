// SPDX-License-Identifier: MPL-2.0
import { describe, expect, it, vi, beforeEach } from "vitest";
import { bridge, type ProjectRef } from "./bridge";
const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));
const project: ProjectRef = {root: "C:/original", project_id: "origin", workspace_format: 1, name: "Original", description: ""};
beforeEach(() => invoke.mockReset());
describe("current GUI contracts", () => {
  it("preserves native picker cancellation", async () => {
    invoke.mockResolvedValue(null);
    expect(await bridge.selectProjectDirectory()).toBeNull();
    expect(invoke).toHaveBeenCalledWith("select_project_directory");
  });
  it("propagates bounded GUI failures without successful project results", async () => {
    invoke.mockResolvedValue({ok: false, error: "Destination already exists"});
    await expect(bridge.createProject("name", "description", "local")).rejects.toThrow("Destination already exists");
  });
  it("binds Apply and Discard to the original project/run", async () => {
    invoke.mockResolvedValue({ok: true, value: {operation_id: "operation"}});
    await bridge.runAction(project, "run", "apply");
    expect(invoke).toHaveBeenLastCalledWith("workspace_request", {method: "gui.run.action", params: {project, run_id: "run", action: "apply", summary: ""}});
    await bridge.runAction(project, "run", "discard", "Retained outcome");
    expect(invoke).toHaveBeenLastCalledWith("workspace_request", {method: "gui.run.action", params: {project, run_id: "run", action: "discard", summary: "Retained outcome"}});
  });
  it("uses conversation identity/cursor and retains separate Update finish", async () => {
    invoke.mockResolvedValue({ok: true, value: {}});
    await bridge.output("conversation-A", "run-A", 3);
    expect(invoke).toHaveBeenLastCalledWith("workspace_request", {method: "gui.conversation.output", params: {conversation_id: "conversation-A", run_id: "run-A", cursor: 3}});
    await bridge.updateFinish("a".repeat(32));
    expect(invoke).toHaveBeenLastCalledWith("finish_update", {operationId: "a".repeat(32)});
  });
});
