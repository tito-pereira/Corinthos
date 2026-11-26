// SPDX-License-Identifier: MPL-2.0
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { ConversationView, type ConversationViewProps } from "./ConversationView";

const mounted: { root: Root; host: HTMLDivElement }[] = [];
const environment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean };

async function render(props: Partial<ConversationViewProps> = {}) {
  environment.IS_REACT_ACT_ENVIRONMENT = true;
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  const onSend = props.onSend ?? vi.fn();
  await act(async () => root.render(<ConversationView
    title="Welcome to Corinthos" messages={[]} busy={false} {...props} onSend={onSend}
  />));
  return { host, root, onSend };
}

async function input(host: HTMLDivElement, value: string) {
  const textarea = host.querySelector("textarea")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(textarea, value);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
  return textarea;
}

afterEach(async () => {
  for (const { root, host } of mounted.splice(0)) {
    await act(async () => root.unmount());
    host.remove();
  }
  environment.IS_REACT_ACT_ENVIRONMENT = false;
});

it("shows the provided welcome and an accessible usable composer with caller controls", async () => {
  const { host, onSend } = await render({
    toolbar: <label>Provider<select aria-label="Provider"><option>Local provider</option></select></label>,
    actions: <button type="button">Apply Changes to Project A</button>,
  });
  expect(host.querySelector("h1")!.textContent).toBe("Welcome to Corinthos");
  expect(host.querySelector("label[for]")!.textContent).toBe("Message");
  expect(host.querySelector("textarea")!.placeholder).toBe("Ask Corinthos…");
  expect(host.querySelector("textarea")!.maxLength).toBe(4096);
  expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
  expect(host.querySelector('select[aria-label="Provider"]')).not.toBeNull();
  expect(host.textContent).toContain("Apply Changes to Project A");
  await input(host, "  Hello Corinthos  ");
  await act(async () => host.querySelector<HTMLButtonElement>('button[type="submit"]')!.click());
  expect(onSend).toHaveBeenCalledExactlyOnceWith("Hello Corinthos");
  expect(host.querySelector("textarea")!.value).toBe("");
});

it("sends with Enter while preserving Shift+Enter and IME composition", async () => {
  const { host, onSend } = await render();
  const textarea = await input(host, "A message\nwith a second line");
  const newline = new KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true, cancelable: true });
  const composing = new KeyboardEvent("keydown", { key: "Enter", isComposing: true, bubbles: true, cancelable: true });
  await act(async () => {
    textarea.dispatchEvent(newline);
    textarea.dispatchEvent(composing);
  });
  expect(onSend).not.toHaveBeenCalled();
  expect(newline.defaultPrevented).toBe(false);
  expect(composing.defaultPrevented).toBe(false);
  const send = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
  await act(async () => textarea.dispatchEvent(send));
  expect(send.defaultPrevented).toBe(true);
  expect(onSend).toHaveBeenCalledExactlyOnceWith("A message\nwith a second line");
});

it("keeps busy cancellation and provider failures visible without sending again", async () => {
  const cancel = vi.fn();
  const { host, onSend } = await render({ busy: true, onCancel: cancel, error: "Provider unavailable" });
  expect(host.querySelector("textarea")!.disabled).toBe(true);
  expect(host.querySelector('[role="status"]')!.textContent).toBe("Corinthos is working…");
  expect(host.querySelector('[role="alert"]')!.textContent).toBe("Provider unavailable");
  const button = Array.from(host.querySelectorAll("button")).find(item => item.textContent === "Cancel")!;
  await act(async () => button.click());
  expect(cancel).toHaveBeenCalledOnce();
  expect(onSend).not.toHaveBeenCalled();
});

it("renders message text safely with role, status, error and truncation observations", async () => {
  const { host } = await render({
    title: "This is the Atlas project page",
    messages: [
      { role: "user", text: "Inspect <script>unsafe()</script>" },
      { role: "assistant", text: "Observed response\nSecond line", status: "FAILED", error: "Process stopped", truncated: true },
    ],
  });
  expect(host.querySelector('[role="log"]')).not.toBeNull();
  expect(host.querySelectorAll("article")).toHaveLength(2);
  expect(host.querySelector("script")).toBeNull();
  expect(host.textContent).toContain("Inspect <script>unsafe()</script>");
  expect(host.textContent).toContain("Observed response\nSecond line");
  expect(host.textContent).toContain("Earlier output omitted from this view.");
  expect(host.querySelector('[role="alert"]')!.textContent).toBe("Process stopped");
  expect(host.textContent).toContain("FAILED");
  expect(host.textContent).not.toContain("Applied");
});
