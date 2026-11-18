// SPDX-License-Identifier: MPL-2.0
import { afterEach, expect, it, vi } from "vitest";
import observer from "./desktop-smoke.js?raw";

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
  document.title = "";
  delete window.__TAURI_INTERNALS__;
});

it.each(["ready", "hidden", "disabled"])("observes delayed Home/Open readiness with a %s directory field", async (fieldState) => {
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
  document.querySelectorAll(".primary-navigation button")[1].onclick = () => {
    screen.innerHTML = "";
    setTimeout(() => {
      screen.innerHTML = '<div class="onboarding-choices"><button disabled>Open existing project</button></div>';
      const choice = screen.querySelector("button");
      choice.onclick = () => {
        choiceClicked();
        screen.innerHTML = `<form><details ${fieldState === "hidden" ? "hidden" : ""}><summary>Enter path manually</summary>
          <input id="project-root" ${fieldState === "disabled" ? "disabled" : ""}></details>
          <button disabled>Open project</button></form>`;
        const form = screen.querySelector("form");
        const input = form.querySelector("input");
        input.oninput = () => { form.querySelector("button").disabled = false; };
        form.onsubmit = (event) => {
          event.preventDefault();
          submitted(input.value, input.closest("details").open);
        };
      };
      setTimeout(() => { choice.disabled = false; }, 100);
    }, 100);
  };

  // Execute the native WebView observer itself, including Settings -> Home navigation.
  Function(`return (${observer});`)()(root, "adaptive-passive");
  await vi.advanceTimersByTimeAsync(100);
  expect(choiceClicked).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(20_500);
  expect(choiceClicked).toHaveBeenCalledOnce();
  if (fieldState === "ready") {
    expect(submitted).toHaveBeenCalledExactlyOnceWith(root, true);
  } else {
    expect(submitted).not.toHaveBeenCalled();
    expect(document.title).toBe(`Corinthos desktop smoke: fail [${fieldState === "disabled" ? "adaptive-open-input" : "unexpected"}]`);
  }
});
