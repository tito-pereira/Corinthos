// SPDX-License-Identifier: MPL-2.0
import { afterEach, expect, it, vi } from "vitest";
import observer from "./desktop-smoke.js?raw";

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
  document.title = "";
  delete window.__TAURI_INTERNALS__;
});

const cases = ["open", "create", "import"].flatMap((flow) =>
  ["ready", "hidden", "disabled"].map((fieldState) => ({ flow, fieldState, previewState: "valid" })),
).concat(["invalid", "registered", "commit-enabled"].map((previewState) =>
  ({ flow: "import", fieldState: "ready", previewState })));

it.each(cases)("observes delayed $flow readiness with a $fieldState field and $previewState preview", async ({ flow, fieldState, previewState }) => {
  vi.useFakeTimers();
  const root = "C:\\fixture\\project";
  const submitted = vi.fn();
  const choiceClicked = vi.fn();
  document.body.innerHTML = `<span class="connection" role="status">Connected</span>
    <ul class="providers"><li><h3>codex</h3><div class="provider-heading"><span>Not yet observed</span></div>
      <p>User setting</p><code>C:\\fixture\\configured\\codex.exe</code></li></ul>
    <nav class="primary-navigation"><button>Settings</button><button>Home</button></nav><main></main>`;
  const screen = document.querySelector("main");
  const runtime = { python_implementation: "CPython", python_version: "3.14.10", supported_schema_version: 9 };
  const snapshot = { connection: "ready", desktop: { version: "0.1.0", identifier: "org.corinthos.desktop" },
    handshake: { version: "0.1.0", protocol_version: 2 } };
  window.__TAURI_INTERNALS__ = { invoke: vi.fn(async (command) => command === "backend_snapshot" ? snapshot : runtime) };
  document.querySelector(".primary-navigation button").onclick = () => {
    screen.innerHTML = `<details class="diagnostics"><summary>About / Diagnostics</summary><section>
      <h2 id="diagnostics-title">Diagnostics</h2><dl>${Object.entries({
        "Application version": "0.1.0", "Desktop version": "0.1.0", "RPC protocol": "v2", "Backend state": "Connected",
        "Private runtime": "CPython 3.14.10", "Supported storage schema": "v9", "Desktop identifier": "org.corinthos.desktop",
      }).map(([label, value]) => `<dt>${label}</dt><dd>${value}</dd>`).join("")}</dl></section></details>`;
  };
  const renderChoice = () => {
    screen.innerHTML = "";
    setTimeout(() => {
      const choiceLabel = { open: "Open existing project", create: "Create new project", import: "Import Git repository" }[flow];
      screen.innerHTML = `<div class="onboarding-choices"><button disabled>${choiceLabel}</button></div>`;
      const choice = screen.querySelector("button");
      choice.onclick = () => {
        choiceClicked();
        const directoryId = flow === "import" ? "import-root" : "project-root";
        const submitLabel = { open: "Open project", create: "Create project", import: "Inspect repository" }[flow];
        screen.innerHTML = `<form class="${flow === "import" ? "repository-import" : ""}">
          ${flow === "create" ? '<input id="project-name">' : ""}
          <details ${fieldState === "hidden" ? "hidden" : ""}><summary>Enter path manually</summary>
          <input id="${directoryId}" ${fieldState === "disabled" ? "disabled" : ""}></details>
          <button disabled>${submitLabel}</button></form>`;
        const form = screen.querySelector("form");
        const input = form.querySelector(`#${directoryId}`);
        for (const field of form.querySelectorAll("input")) field.oninput = () => {
          form.querySelector("button").disabled = !input.value ||
            (flow === "create" && !form.querySelector("#project-name").value);
        };
        form.onsubmit = (event) => {
          event.preventDefault();
          submitted(input.value, input.closest("details").open);
          if (flow === "import") {
            screen.innerHTML = `<section class="repository-import"><div class="import-preview"><dl>
              ${Object.entries({ "Git root": root, "Working tree": "Dirty",
                "Tracked files": previewState === "invalid" ? "4" : "5", "Git object format": "SHA-256",
                HEAD: "revision", "Discovery digest": "a".repeat(64),
              }).map(([label, value]) => `<dt>${label}</dt><dd>${value}</dd>`).join("")}
              </dl><p>DISCOVERED module candidates pyproject.toml README.md tests/test_payments.py</p></div>
              <button ${previewState === "commit-enabled" ? "" : "disabled"}>Import repository</button></section>
              ${previewState === "registered" ? `<ul class="project-list"><li><code>${root}</code></li></ul>` : ""}`;
          }
        };
      };
      setTimeout(() => { choice.disabled = false; }, 100);
    }, 100);
  };
  document.querySelectorAll(".primary-navigation button")[1].onclick = renderChoice;
  if (flow === "create") renderChoice();

  // Execute the native observer itself through its real onboarding entry point.
  Function(`return (${observer});`)()(root, { open: "adaptive-passive", create: "create", import: "import-preview" }[flow]);
  await vi.advanceTimersByTimeAsync(100);
  expect(choiceClicked).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(fieldState === "disabled" ? 20_500 : previewState === "invalid" ? 50_500 : 500);
  expect(choiceClicked).toHaveBeenCalledOnce();
  if (fieldState === "ready") {
    expect(submitted).toHaveBeenCalledExactlyOnceWith(root, true);
    if (flow === "import") {
      const result = { valid: "pass", invalid: "fail [import-preview-values]",
        registered: "fail [import-preview-state]", "commit-enabled": "fail [import-preview-state]" }[previewState];
      expect(document.title).toBe(`Corinthos desktop smoke: ${result}`);
    }
  } else {
    expect(submitted).not.toHaveBeenCalled();
    const disabledStage = { open: "adaptive-open-input", create: "create-form", import: "import-input" }[flow];
    expect(document.title).toBe(`Corinthos desktop smoke: fail [${fieldState === "disabled" ? disabledStage : "unexpected"}]`);
  }
});
