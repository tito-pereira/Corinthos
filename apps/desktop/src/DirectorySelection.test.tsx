// SPDX-License-Identifier: MPL-2.0
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DirectorySelection } from "./DirectorySelection";

const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));

let host: HTMLDivElement;
let root: Root;
let finish: ((value: string | null) => void) | undefined;
const change = vi.fn();
const busy = vi.fn();
const env = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean };

beforeEach(() => {
  env.IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  finish = undefined;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => finish?.(null));
  await act(async () => root.unmount());
  host.remove();
  env.IS_REACT_ACT_ENVIRONMENT = false;
});

async function render(value = "C:\\original", context = "open") {
  await act(async () => root.render(<DirectorySelection id="directory" label="Project root"
    value={value} context={context} disabled={false} onChange={change} onPickingChange={busy} />));
}

async function select() {
  await act(async () => host.querySelector<HTMLButtonElement>("button")!.click());
}

function defer() {
  invoke.mockReturnValue(new Promise<string | null>((resolve) => { finish = resolve; }));
}

describe("shared directory selection", () => {
  it("returns an explicitly chosen path without a domain request", async () => {
    invoke.mockResolvedValue("C:\\Projects\\Olá world");
    await render();
    await select();
    expect(change).toHaveBeenCalledWith("C:\\Projects\\Olá world");
    expect(invoke.mock.calls).toEqual([["select_project_directory"]]);
    expect(busy.mock.calls).toEqual([[true], [false]]);
  });

  it("preserves the draft on cancellation or selecting the same directory", async () => {
    invoke.mockResolvedValueOnce(null).mockResolvedValueOnce("C:\\original");
    await render();
    await select();
    await select();
    expect(change).not.toHaveBeenCalled();
    expect(host.querySelector<HTMLInputElement>("input")!.value).toBe("C:\\original");
    expect(host.querySelector('[role="alert"]')).toBeNull();
  });

  it("retains the manual fallback and hides native details after unavailable selection", async () => {
    invoke.mockRejectedValue("private native details");
    await render();
    const details = host.querySelector("details")!;
    expect(details.open).toBe(false);
    await select();
    expect(host.textContent).toContain("Folder selection is unavailable or failed");
    expect(host.textContent).not.toContain("private native details");
    expect(host.querySelector("summary")!.textContent).toBe("Enter path manually");
    expect(host.querySelector<HTMLInputElement>("input")!.disabled).toBe(false);
    expect(change).not.toHaveBeenCalled();
    expect(details.open).toBe(false);
    await act(async () => details.querySelector("summary")!.click());
    expect(details.open).toBe(true);
    const input = host.querySelector<HTMLInputElement>("input")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "C:\\manual fallback");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(change).toHaveBeenCalledExactlyOnceWith("C:\\manual fallback");
  });

  it.each(["path", "flow"])("ignores a late result after the %s changes", async (changed) => {
    defer();
    await render();
    await select();
    await render(changed === "path" ? "C:\\edited" : "C:\\original", changed === "flow" ? "create" : "open");
    await act(async () => finish!("C:\\late"));
    expect(change).not.toHaveBeenCalled();
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(busy.mock.calls).toEqual([[true], [false]]);
  });

  it("ignores a late result after unmount", async () => {
    defer();
    await render();
    await select();
    await act(async () => root.render(null));
    await act(async () => finish!("C:\\late"));
    expect(change).not.toHaveBeenCalled();
    expect(busy.mock.calls).toEqual([[true], [false]]);
  });

  it("suppresses simultaneous picker requests from different flows", async () => {
    defer();
    await act(async () => root.render(<>
      <DirectorySelection id="project" label="Project root" value="" context="open" disabled={false}
        onChange={change} onPickingChange={busy} />
      <DirectorySelection id="import" label="Repository" value="" context="import" disabled={false}
        onChange={vi.fn()} onPickingChange={vi.fn()} />
    </>));
    await act(async () => {
      host.querySelectorAll<HTMLButtonElement>("button")[0].click();
      host.querySelectorAll<HTMLButtonElement>("button")[1].click();
    });
    expect(invoke).toHaveBeenCalledTimes(1);
    await act(async () => finish!("C:\\selected"));
    expect(change).toHaveBeenCalledOnce();
  });
});
