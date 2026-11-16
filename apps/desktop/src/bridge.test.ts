// SPDX-License-Identifier: MPL-2.0
import { beforeEach, describe, expect, it, vi } from "vitest";
import { bridge, ProjectCreationError } from "./bridge";

const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));

beforeEach(() => vi.resetAllMocks());

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
