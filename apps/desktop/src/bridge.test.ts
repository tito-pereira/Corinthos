// SPDX-License-Identifier: MPL-2.0
import { beforeEach, describe, expect, it, vi } from "vitest";
import { bridge, ProjectCreationError } from "./bridge";

const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));

beforeEach(() => vi.resetAllMocks());

describe("project onboarding transport", () => {
  it("binds explicit opening and migration to the inspected root and digest", async () => {
    const inspection = { root: "C:\\project", status: "CURRENT", inspection_digest: "digest" };
    invoke.mockResolvedValueOnce(inspection).mockResolvedValueOnce({ root: inspection.root }).mockResolvedValueOnce({ root: inspection.root });
    expect(await bridge.inspectProject(inspection.root)).toBe(inspection);
    await bridge.openInspectedProject(inspection.root, inspection.inspection_digest);
    await bridge.openInspectedProject(inspection.root, inspection.inspection_digest, true);
    expect(invoke.mock.calls).toEqual([
      ["workspace_request", { method: "project.inspect", params: { root: inspection.root } }],
      ["workspace_request", { method: "project.open_checked", params: { root: inspection.root, inspection_digest: "digest", migrate: false } }],
      ["workspace_request", { method: "project.open_checked", params: { root: inspection.root, inspection_digest: "digest", migrate: true } }],
    ]);
  });
});

describe("native directory selection transport", () => {
  it("returns cancellation separately from a native failure", async () => {
    invoke.mockResolvedValueOnce(null);
    expect(await bridge.selectProjectDirectory()).toBeNull();
    invoke.mockRejectedValueOnce("DIRECTORY_PICKER_UNAVAILABLE");
    await expect(bridge.selectProjectDirectory()).rejects.toBe("DIRECTORY_PICKER_UNAVAILABLE");
    expect(invoke.mock.calls).toEqual([["select_project_directory"], ["select_project_directory"]]);
  });

  it("opens independently while the serialized Python request queue is pending", async () => {
    let finish: (value: object) => void = () => {};
    invoke.mockImplementation((command) => command === "workspace_request"
      ? new Promise((resolve) => { finish = resolve; }) : Promise.resolve("C:\\selected"));
    const opening = bridge.project("C:\\project", true);
    await Promise.resolve();
    expect(await bridge.selectProjectDirectory()).toBe("C:\\selected");
    expect(invoke.mock.calls.map(([command]) => command)).toEqual(["workspace_request", "select_project_directory"]);
    finish({ root: "C:\\project" });
    await opening;
  });
});

describe("project creation transport", () => {
  it("sends exactly the selected root and display name and returns the backend summary", async () => {
    const summary = { root: "C:\\projects\\one", display_name: "My project", initialized: true };
    invoke.mockResolvedValueOnce(summary);
    expect(await bridge.createProject(summary.root, summary.display_name)).toBe(summary);
    expect(invoke.mock.calls).toEqual([["workspace_request", {
      method: "project.create", params: { root: summary.root, display_name: summary.display_name },
    }]]);
  });

  it.each(["INVALID_INPUT", "CONFLICT", "ACCESS_DENIED", "REGISTRY_INCOMPLETE", "STATE_UNAVAILABLE"] as const)(
    "retains the bounded %s recovery outcome", async (creationCode) => {
      invoke.mockRejectedValueOnce(`PROJECT_CREATE_${creationCode}`);
      await expect(bridge.createProject("C:\\project", "Name")).rejects.toMatchObject({
        name: "ProjectCreationError", creationCode,
      });
      expect(invoke).toHaveBeenCalledTimes(1);
    },
  );

  it("keeps a lost response unconfirmed without retrying or exposing native details", async () => {
    invoke.mockRejectedValueOnce("private native diagnostic");
    const error = await bridge.createProject("C:\\project", "Name").catch((failure) => failure);
    expect(error).toBeInstanceOf(ProjectCreationError);
    expect(error.creationCode).toBe("UNCONFIRMED");
    expect(error.message).not.toContain("private native diagnostic");
    invoke.mockResolvedValueOnce({ projects: [], next_offset: null });
    await bridge.projects(0);
    expect(invoke.mock.calls.map(([, args]) => args.method)).toEqual(["project.create", "projects.list"]);
  });
});
