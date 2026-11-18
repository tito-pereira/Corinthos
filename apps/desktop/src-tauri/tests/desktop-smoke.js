// SPDX-License-Identifier: MPL-2.0
(function (projectRoot, mode) {
  const report = (result) => {
    document.title = "Corinthos desktop smoke: " + result;
  };

  const waitFor = async (predicate, timeout = 20_000) => {
    const deadline = performance.now() + timeout;
    while (performance.now() < deadline) {
      if (predicate()) return true;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return false;
  };

  const text = (node) => node?.textContent.trim();
  const valueFor = (container, name) => {
    const label = Array.from(container?.querySelectorAll("dt") ?? []).find(
      (item) => text(item) === name,
    );
    return text(label?.nextElementSibling);
  };

  const normalizeWindowsPath = (path) => path.replaceAll("/", "\\").toLowerCase();

  const setInput = (field, value) => {
    if (!field?.matches(":enabled") || field.closest("details:not([open]), [hidden]")) {
      throw new Error("Cannot edit a hidden or disabled smoke field.");
    }
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), "value").set;
    setter.call(field, value);
    field.dispatchEvent(new Event(field instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  };

  const enterDirectory = (field, value) => {
    const disclosure = field?.closest("details");
    if (!disclosure) throw new Error("The directory field has no manual-entry disclosure.");
    if (!disclosure.open) disclosure.querySelector("summary").click();
    setInput(field, value);
  };

  const buttonNamed = (selector, label) => Array.from(document.querySelectorAll(selector)).find(
    (button) => text(button) === label,
  );
  const view = (name) => buttonNamed(".project-views button", name === "Work Items" ? "Work" : name);
  const sha256 = (value) => /^[a-f0-9]{64}$/.test(value ?? "");
  const revealsSecret = (selector) => (text(document.querySelector(selector)) ?? "").includes(
    "current-fixture-hidden-value",
  );
  const adaptiveTask = "Adaptive fixture: add one local check";
  const adaptiveProject = "Adaptive fixture: inspect project planning";
  const importChange = "Import fixture: review payments";
  const importedOutput = "Observed the imported payments context marker.";

  function importedRow() {
    return Array.from(document.querySelectorAll(".project-list li")).find((row) => {
      const path = text(row.querySelector("code"));
      return path && normalizeWindowsPath(path) === normalizeWindowsPath(projectRoot);
    });
  }

  async function verifyImport() {
    const panel = () => document.querySelector(".import-preview");
    const formButton = (label) => buttonNamed(".repository-import button", label);
    const project = () => document.querySelector(".project-result");
    if (mode !== "import-execute") {
      buttonNamed(".onboarding-choices button", "Import Git repository")?.click();
      if (!(await waitFor(() => document.querySelector("#import-root")?.matches(":enabled")))) {
        return report("fail [import-preview]");
      }
      const form = document.querySelector("#import-root");
      if (!form?.matches(":enabled")) return report("fail [import-preview]");
      enterDirectory(form, projectRoot);
      if (!(await waitFor(() => formButton("Inspect repository")?.matches(":enabled")))) {
        return report("fail [import-preview]");
      }
      formButton("Inspect repository").form.requestSubmit(formButton("Inspect repository"));
      if (!(await waitFor(() => {
        const root = valueFor(panel(), "Git root");
        const preview = text(panel());
        return root && normalizeWindowsPath(root) === normalizeWindowsPath(projectRoot) &&
          valueFor(panel(), "Working tree") === "Dirty" &&
          valueFor(panel(), "Tracked files") === "5" &&
            valueFor(panel(), "Git object format") === "SHA-256" &&
          Boolean(valueFor(panel(), "HEAD")) && sha256(valueFor(panel(), "Discovery digest")) &&
          preview.includes("DISCOVERED module candidates") && preview.includes("pyproject.toml") &&
          preview.includes("README.md") && preview.includes("tests/test_payments.py");
      }, 50_000))) return report("fail [import-preview]");
      if (importedRow() || !formButton("Import repository")?.disabled) {
        return report("fail [import-preview]");
      }
      if (mode === "import-preview") return report("pass");
      for (const confirmation of panel().querySelectorAll('input[type="checkbox"]')) {
        confirmation.click();
        if (!confirmation.checked) return report("fail [import-commit]");
      }
      if (!(await waitFor(() => formButton("Import repository")?.matches(":enabled")))) {
        return report("fail [import-commit]");
      }
      formButton("Import repository").click();
      if (!(await waitFor(() => sha256(text(document.querySelector(".import-confirmed code"))), 125_000))) {
        return report("fail [import-commit]");
      }
    } else {
      if (!(await waitFor(() => importedRow()?.querySelector("button")?.matches(":enabled")))) {
        return report("fail [import-project]");
      }
      importedRow().querySelector("button").click();
    }
    if (!(await waitFor(() => {
      const selected = text(project()?.querySelector("code"));
      return importedRow() && selected && normalizeWindowsPath(selected) === normalizeWindowsPath(projectRoot) &&
        valueFor(project(), "Initialized") === "Yes" && valueFor(project(), "Storage") === "Current · v9" &&
        valueFor(project(), "Project Contract") === undefined && valueFor(project(), "Workflow") === undefined;
    }))) return report("fail [import-project]");
    if (!document.querySelector("#work-objective")) view("Work")?.click();
    if (!(await waitFor(() => document.querySelector("#work-objective")?.matches(":enabled")))) {
      return report("fail [import-project]");
    }
    if (mode === "import-commit") return report("pass");

    const adaptive = () => document.querySelector(".adaptive-workflow");
    const adaptiveAction = (label) => buttonNamed(".adaptive-workflow button", label);
    const create = () => buttonNamed(".work-form button", "Create Work Item");
    setInput(document.querySelector("#work-objective"), importChange);
    setInput(document.querySelector("#work-scope"), "CHANGE");
    setInput(document.querySelector("#work-criteria"), "Verify the payments service regression checks.");
    if (!(await waitFor(() => create()?.matches(":enabled")))) return report("fail [import-work]");
    create().form.requestSubmit(create());
    if (!(await waitFor(() => valueFor(adaptive(), "Selected scope") === "CHANGE" &&
      valueFor(adaptive(), "Stage") === "READY" && document.querySelector("#adaptive-provider")?.matches(":enabled")))) {
      return report("fail [import-work]");
    }
    setInput(document.querySelector("#adaptive-provider"), "codex");
    const start = () => adaptiveAction("Start adaptive workflow");
    if (!(await waitFor(() => start()?.matches(":enabled")))) return report("fail [import-session]");
    start().form.requestSubmit(start());
    if (!(await waitFor(() => valueFor(adaptive(), "Latest session")?.startsWith("session-") &&
      sha256(valueFor(adaptive(), "Projection digest")) && adaptiveAction("Refresh workflow")?.matches(":enabled")))) {
      return report("fail [import-session]");
    }
    if (!(await waitFor(() => {
      if (valueFor(adaptive(), "Session state") === "SUCCEEDED") return true;
      const refresh = adaptiveAction("Refresh workflow");
      if (refresh?.matches(":enabled")) refresh.click();
      return false;
    }))) return report("fail [import-session]");
    if (valueFor(adaptive(), "Validation") !== "NOT_VERIFIED" || adaptiveAction("Complete adaptive workflow")) {
      return report("fail [import-session]");
    }
    const sessionId = valueFor(adaptive(), "Latest session");
    const projectionDigest = valueFor(adaptive(), "Projection digest");
    const sessionButton = () => buttonNamed(".agents .session-list button", sessionId);
    view("Agents").click();
    if (!(await waitFor(() => sessionButton()?.matches(":enabled")))) return report("fail [import-session]");
    sessionButton().click();
    if (!(await waitFor(() => {
      const detail = document.querySelector(".session-detail");
      return valueFor(detail, "Context digest") === projectionDigest && valueFor(detail, "State") === "SUCCEEDED" &&
        text(detail?.querySelector(".session-events"))?.includes(importedOutput);
    }))) return report("fail [import-context]");
    return report("pass");
  }

  async function verifyAdaptive() {
    const summary = () => document.querySelector(".project-result");
    const panel = () => document.querySelector(".adaptive-workflow");
    const facts = (label) => valueFor(panel(), label);
    const action = (label) => buttonNamed(".adaptive-workflow button", label);
    const create = () => buttonNamed(".work-form button", "Create Work Item");
    const treeItem = (objective) => buttonNamed(".work-items .work-tree button", objective);
    const openChoice = () => buttonNamed(".onboarding-choices button", "Open existing project");
    if (!(await waitFor(() => openChoice()?.matches(":enabled")))) return report("fail [adaptive-open-choice]");
    openChoice().click();
    if (!(await waitFor(() => document.querySelector("#project-root")?.matches(":enabled")))) {
      return report("fail [adaptive-open-input]");
    }
    const projectForm = document.querySelector("#project-root");
    enterDirectory(projectForm, projectRoot);
    const openProject = () => buttonNamed("form button", "Open project");
    if (!(await waitFor(() => openProject()?.matches(":enabled")))) return report("fail [adaptive-open-submit]");
    openProject().form.requestSubmit(openProject());
    if (!(await waitFor(() => {
      const path = text(summary()?.querySelector("code"));
      return path && normalizeWindowsPath(path) === normalizeWindowsPath(projectRoot);
    }))) return report("fail [adaptive-open-project]");
    if (valueFor(summary(), "Storage") !== "Current · v9") return report("fail [adaptive-storage]");
    if (!document.querySelector("#work-objective")) view("Work")?.click();
    if (!(await waitFor(() => document.querySelector("#work-objective")?.matches(":enabled")))) {
      return report("fail [adaptive-work-view]");
    }

    if (mode === "adaptive-passive") {
      async function createWork(objective, scope) {
        setInput(document.querySelector("#work-objective"), objective);
        setInput(document.querySelector("#work-scope"), scope);
        setInput(document.querySelector("#work-criteria"), "Keep existing contracts unchanged.");
        if (!(await waitFor(() => create()?.matches(":enabled")))) return false;
        create().form.requestSubmit(create());
        return waitFor(() => treeItem(objective)?.matches(":enabled"));
      }
      if (!(await createWork(adaptiveProject, "PROJECT"))) return report("fail [adaptive-create-project]");
      if (!(await waitFor(() =>
        facts("Selected scope") === "PROJECT" && facts("Stage") === "PLANNING_REQUIRED" &&
        facts("Validation") !== "VERIFIED" && !action("Start adaptive workflow") &&
        text(panel())?.includes("corinthos workflow plan"),
      ))) return report("fail [adaptive-planning-gate]");
      if (!(await createWork(adaptiveTask, "TASK"))) return report("fail [adaptive-task]");
    } else {
      if (!(await waitFor(() => treeItem(adaptiveTask)?.matches(":enabled")))) {
        return report("fail [adaptive-task]");
      }
      treeItem(adaptiveTask).click();
    }
    if (!(await waitFor(() =>
      facts("Selected scope") === "TASK" && facts("Recommended scope") === "TASK" &&
      facts("Minimum required scope") === "TASK" && facts("Stage") === "READY" &&
      document.querySelector("#adaptive-provider")?.matches(":enabled"),
    ))) return report("fail [adaptive-task]");

    if (mode === "adaptive-passive") {
      action("Show Work Item report").click();
      if (!(await waitFor(() => text(panel()?.querySelector(".adaptive-report p")) === adaptiveTask))) {
        return report("fail [adaptive-report]");
      }
      return report("pass");
    }

    setInput(document.querySelector("#adaptive-provider"), "codex");
    if (!(await waitFor(() => action("Start adaptive workflow")?.matches(":enabled")))) {
      return report("fail [adaptive-session]");
    }
    action("Start adaptive workflow").form.requestSubmit(action("Start adaptive workflow"));
    if (!(await waitFor(() =>
      facts("Latest session")?.startsWith("session-") && facts("Session provider") === "codex" &&
      facts("Latest projection")?.startsWith("projection-") && sha256(facts("Projection digest")) &&
      action("Refresh workflow")?.matches(":enabled"),
    ))) return report("fail [adaptive-session]");
    if (!(await waitFor(() => {
      if (facts("Session state") === "SUCCEEDED") return true;
      const refresh = action("Refresh workflow");
      if (refresh?.matches(":enabled")) refresh.click();
      return false;
    }))) {
      return report("fail [adaptive-session]");
    }
    if (!(await waitFor(() =>
      action("Show Work Item report")?.matches(":enabled") && !action("Complete adaptive workflow"),
    ))) return report("fail [adaptive-report]");
    action("Show Work Item report").click();
    if (!(await waitFor(() => {
      const reportPanel = panel()?.querySelector(".adaptive-report");
      return text(reportPanel?.querySelector("p")) === adaptiveTask &&
        valueFor(reportPanel, "Validation") === "NOT_VERIFIED" &&
        valueFor(summary(), "Workflow") === undefined;
    }))) return report("fail [adaptive-report]");
    return report("pass");
  }

  async function verifyCommand() {
    const sibling = (name) => projectRoot.replace(/[^\\/]+$/, name);
    const otherRoot = sibling("command-project");
    const objective = "Command fixture: review payments";
    const otherObjective = "Command fixture: plan Project B";
    const primary = (name) => buttonNamed('nav[aria-label="Primary navigation"] button', name);
    const within = (node, label) => Array.from(node?.querySelectorAll("button") ?? []).find(
      (button) => text(button) === label && button.matches(":enabled"),
    );
    const samePath = (node, expected) => {
      const path = text(node);
      return path && normalizeWindowsPath(path) === normalizeWindowsPath(expected);
    };
    const card = (root) => Array.from(document.querySelectorAll(".command-card")).find(
      (candidate) => samePath(candidate.querySelector(".command-card-root"), root),
    );
    const a = () => card(projectRoot);
    const b = () => card(otherRoot);
    const focus = (container, workObjective) => Array.from(container?.querySelectorAll(".command-focus li") ?? []).find(
      (item) => text(item.querySelector("strong")) === workObjective,
    );
    const attention = () => Array.from(b()?.querySelectorAll(".command-attention li") ?? []).find(
      (item) => text(item.querySelector("code")) === "PLANNING_REQUIRED",
    );
    const fact = (name) => valueFor(a(), name);
    const project = (root) => samePath(document.querySelector(".project-result code"), root);
    const active = (sessionId, provider) => fact("Active session") === sessionId &&
      fact("Provider") === provider && fact("Session state") === "RUNNING";
    if (!(await waitFor(() => primary("Command")?.matches(":enabled")))) return report("fail [command-cards]");
    primary("Command").click();
    if (!(await waitFor(() => fact("Availability") === "AVAILABLE" && sha256(fact("Import snapshot")) &&
      valueFor(b(), "Availability") === "AVAILABLE" && valueFor(b(), "Validation") === "NOT_VERIFIED" &&
      focus(b(), otherObjective) && within(attention(), "Open Attention") &&
      valueFor(card(sibling("missing-project")), "Availability") === "MISSING" &&
      valueFor(card(sibling("command-invalid-project")), "Availability") === "UNAVAILABLE" &&
      text(card(sibling("command-invalid-project"))?.querySelector("[role=alert] code")) === "PROJECT_STATE_UNAVAILABLE"
    ))) return report("fail [command-cards]");
    const bBefore = text(b());
    const bWorkId = text(focus(b(), otherObjective)?.querySelector("code"));
    if (!bWorkId || valueFor(b(), "Active session")) return report("fail [command-cards]");

    if (mode === "command-passive") {
      const observed = () => document.querySelector("time.command-observed")?.dateTime;
      if (!(await waitFor(() => observed()))) return report("fail [command-read]");
      const baselineObservation = observed();
      const aBefore = text(a());
      if (!(await waitFor(() => observed() && observed() !== baselineObservation))) return report("fail [command-read]");
      const periodicObservation = observed();
      const refresh = buttonNamed("button", "Refresh Command");
      if (!refresh?.matches(":enabled")) return report("fail [command-read]");
      refresh.click();
      if (!(await waitFor(() => observed() && observed() !== periodicObservation)) ||
          text(a()) !== aBefore || text(b()) !== bBefore) return report("fail [command-read]");
      return report("pass");
    }

    within(a(), "Open Project")?.click();
    if (!(await waitFor(() => project(projectRoot) && view("Work")?.matches(":enabled")))) {
      return report("fail [command-start]");
    }
    view("Work").click();
    if (!(await waitFor(() => document.querySelector("#work-objective")?.matches(":enabled")))) {
      return report("fail [command-start]");
    }
    setInput(document.querySelector("#work-objective"), objective);
    setInput(document.querySelector("#work-scope"), "CHANGE");
    setInput(document.querySelector("#work-criteria"), "Verify the payments service regression checks.");
    const create = () => buttonNamed(".work-form button", "Create Work Item");
    if (!(await waitFor(() => create()?.matches(":enabled")))) return report("fail [command-start]");
    create().form.requestSubmit(create());
    const workflow = () => document.querySelector(".adaptive-workflow");
    const workflowFact = (name) => valueFor(workflow(), name);
    if (!(await waitFor(() => workflowFact("Selected scope") === "CHANGE" && workflowFact("Stage") === "READY" &&
      document.querySelector("#adaptive-provider")?.matches(":enabled")))) return report("fail [command-start]");
    setInput(document.querySelector("#adaptive-provider"), "codex");
    const start = () => within(workflow(), "Start adaptive workflow");
    if (!(await waitFor(start))) return report("fail [command-start]");
    start().form.requestSubmit(start());
    if (!(await waitFor(() => {
      if (workflowFact("Session state") === "RUNNING" && workflowFact("Latest session") &&
          sha256(workflowFact("Projection digest"))) return true;
      within(workflow(), "Refresh workflow")?.click();
      return false;
    }))) return report("fail [command-start]");
    const sourceId = workflowFact("Latest session");
    const sourceDigest = workflowFact("Projection digest");
    primary("Command").click();
    if (!(await waitFor(() => active(sourceId, "codex") && within(attention(), "Open Attention") &&
      within(focus(a(), objective), "Open Work Item")))) return report("fail [command-work]");
    const aWorkId = text(focus(a(), objective)?.querySelector("code"));
    if (!aWorkId || aWorkId === bWorkId) return report("fail [command-work]");

    within(attention(), "Open Attention").click();
    if (!(await waitFor(() => project(otherRoot) && workflowFact("Stage") === "PLANNING_REQUIRED" &&
      valueFor(document.querySelector(".work-selection"), "Work Item ID") === bWorkId &&
      text(document.querySelector(".work-selection strong")) === otherObjective &&
      within(workflow(), "Open Validation")))) return report("fail [command-attention]");
    within(workflow(), "Open Validation").click();
    if (!(await waitFor(() => project(otherRoot) &&
      text(document.querySelector(".validation-view .work-selection code")) === bWorkId &&
      text(document.querySelector(".validation-view .work-selection [data-status]")) === "NOT_VERIFIED"
    ))) return report("fail [command-attention]");
    primary("Command").click();
    if (!(await waitFor(() => active(sourceId, "codex") && text(b()) === bBefore &&
      within(focus(a(), objective), "Open Work Item")))) return report("fail [command-work]");
    within(focus(a(), objective), "Open Work Item").click();
    const work = () => document.querySelector(".work-items .work-selection");
    if (!(await waitFor(() => project(projectRoot) && text(work()?.querySelector("strong")) === objective &&
      valueFor(work(), "Work Item ID") === aWorkId && within(work(), "Open Agent Session")))) {
      return report("fail [command-work]");
    }
    within(work(), "Open Agent Session").click();
    const session = () => document.querySelector(".session-detail");
    if (!(await waitFor(() => text(session()?.querySelector("h4")) === sourceId &&
      valueFor(session(), "State") === "RUNNING" && valueFor(session(), "Provider") === "codex" &&
      valueFor(session(), "Work Item") === aWorkId && valueFor(session(), "Context digest") === sourceDigest &&
      text(session()?.querySelector(".session-events"))?.includes("Deterministic session output") &&
      within(session(), "Open Context Projection")))) return report("fail [command-agent]");
    const sourceProjection = valueFor(session(), "Context projection");
    within(session(), "Open Context Projection").click();
    const projection = () => document.querySelector(".projection-detail");
    if (!(await waitFor(() => valueFor(projection(), "Projection") === sourceProjection &&
      valueFor(projection(), "Work Item") === aWorkId &&
      valueFor(projection(), "Projection digest") === sourceDigest &&
      Array.from(projection()?.querySelectorAll(".context-sources li") ?? []).some((row) =>
        valueFor(row, "Identity") === "src/payments.py" && text(row.querySelector("strong"))?.includes("OPERATIONAL"))
    ))) return report("fail [command-context]");

    primary("Command").click();
    if (!(await waitFor(() => active(sourceId, "codex") && within(a(), "Switch Provider") &&
      text(b()) === bBefore))) return report("fail [command-switch]");
    within(a(), "Switch Provider").click();
    const switchPanel = () => document.querySelector(".provider-switch");
    const switchFact = (name) => valueFor(switchPanel(), name);
    const switchAction = (name) => within(switchPanel(), name);
    const switchStage = () => text(switchPanel()?.querySelector(".switch-stage"));
    const targetField = () => switchPanel()?.querySelector("#switch-target-provider");
    if (!(await waitFor(() => project(projectRoot) && text(session()?.querySelector("h4")) === sourceId &&
      targetField()?.matches(":enabled")))) return report("fail [command-switch]");
    setInput(targetField(), "claude");
    if (!(await waitFor(() => switchStage() === "SOURCE_ACTIVE" && switchAction("Switch provider")))) {
      return report("fail [command-switch]");
    }
    switchAction("Switch provider").click();
    if (!(await waitFor(() => switchStage() === "SOURCE_TERMINAL" && switchFact("Source state") === "CANCELLED" &&
      switchAction("Continue switch")))) return report("fail [command-switch]");
    switchAction("Continue switch").click();
    if (!(await waitFor(() => switchStage() === "TARGET_SESSION_ACTIVE" && switchFact("Target state") === "RUNNING" &&
      switchFact("Target session") && switchFact("Target session") !== sourceId && switchFact("Source provider") === "codex" &&
      switchFact("Work Item") === aWorkId && switchFact("Source Handoff") && sha256(switchFact("Handoff digest")) &&
      switchFact("Target Projection") && switchFact("Target Projection") !== sourceProjection &&
      sha256(switchFact("Projection digest")) && switchAction("Replay switch")))) return report("fail [command-switch]");
    const targetId = switchFact("Target session");
    switchAction("Replay switch").click();
    if (!(await waitFor(() => switchFact("Target session") === targetId &&
      text(switchPanel()?.querySelector(".switch-replay-confirmed")) === "Existing target session confirmed."))) {
      return report("fail [command-replay]");
    }
    primary("Command").click();
    if (!(await waitFor(() => active(targetId, "claude") && text(b()) === bBefore &&
      text(focus(a(), objective)?.querySelector("code")) === aWorkId))) return report("fail [command-switch]");
    return report("pass");
  }

  async function verifyCreation() {
    const primary = (name) => buttonNamed(".primary-navigation button", name);
    const create = () => buttonNamed("button", "Create new project");
    if (!(await waitFor(() => text(document.querySelector(".connection[role=status]")) === "Connected" &&
      create()?.matches(":enabled")))) return report("fail [backend]");
    create().click();
    if (!(await waitFor(() => document.querySelector("#project-name")?.matches(":enabled")))) {
      return report("fail [create-form]");
    }
    setInput(document.querySelector("#project-name"), "Packaged creation project");
    enterDirectory(document.querySelector("#project-root"), projectRoot);
    const submit = () => buttonNamed("form button", "Create project");
    if (!(await waitFor(() => submit()?.matches(":enabled")))) return report("fail [create-form]");
    submit().form.requestSubmit(submit());
    const summary = () => document.querySelector(".project-result");
    if (!(await waitFor(() => text(summary()?.querySelector("h3")) === "Packaged creation project" &&
      valueFor(summary(), "Initialized") === "Yes" && valueFor(summary(), "Storage") === "No database"))) {
      return report("fail [create-result]");
    }
    if (valueFor(summary(), "Project Contract") || valueFor(summary(), "Workflow")) {
      return report("fail [create-result]");
    }
    primary("Home").click();
    if (!(await waitFor(() => Array.from(document.querySelectorAll(".project-list h3")).some(
      (node) => text(node) === "Packaged creation project")))) return report("fail [create-registry]");
    primary("Command").click();
    if (!(await waitFor(() => Array.from(document.querySelectorAll(".command-card")).some(
      (card) => text(card.querySelector("h3")) === "Packaged creation project" &&
        normalizeWindowsPath(text(card.querySelector(".command-card-root"))) === normalizeWindowsPath(projectRoot)
    )))) return report("fail [create-command]");
    return report("pass");
  }

  async function verify() {
    if (mode === "create") return verifyCreation();
    const primary = (name) => buttonNamed(".primary-navigation button", name);
    const backendReady = await waitFor(() => {
      const codex = Array.from(document.querySelectorAll(".providers li")).find(
        (row) => text(row.querySelector("h3")) === "codex",
      );
      const configuredPath = text(codex?.querySelector("code"));
      return (
        text(document.querySelector(".connection[role=status]")) === "Connected" &&
        text(codex?.querySelector(".provider-heading span")) === "Not yet observed" &&
        text(codex?.querySelector("p")) === "User setting" &&
        configuredPath &&
        normalizeWindowsPath(configuredPath).endsWith("\\configured\\codex.exe")
      );
    });
    if (!backendReady) return report("fail [backend]");
    primary("Settings").click();
    if (!(await waitFor(() => document.querySelector("details.diagnostics summary")))) return report("fail [backend]");
    const disclosure = document.querySelector("details.diagnostics summary");
    disclosure.click();
    if (!(await waitFor(() => /^\w+ \d+\.\d+\.\d+$/.test(valueFor(
      document.querySelector("#diagnostics-title")?.closest("section"), "Private runtime"))))) return report("fail [backend]");
    const invoke = window.__TAURI_INTERNALS__?.invoke;
    if (!invoke) return report("fail [backend]");
    const snapshot = await invoke("backend_snapshot");
    const runtime = await invoke("workspace_request", { method: "system.diagnostics", params: {} });
    if (snapshot.connection !== "ready") return report("fail [backend]");
    if (!(await waitFor(() => {
      const diagnostics = document.querySelector("#diagnostics-title")?.closest("section");
      return valueFor(diagnostics, "Application version") === snapshot.handshake.version &&
        valueFor(diagnostics, "Desktop version") === snapshot.desktop.version &&
        valueFor(diagnostics, "RPC protocol") === `v${snapshot.handshake.protocol_version}` &&
        valueFor(diagnostics, "Backend state") === "Connected" &&
        valueFor(diagnostics, "Private runtime") === `${runtime.python_implementation} ${runtime.python_version}` &&
        valueFor(diagnostics, "Supported storage schema") === `v${runtime.supported_schema_version}` &&
        valueFor(diagnostics, "Desktop identifier") === snapshot.desktop.identifier;
    }))) return report("fail [backend]");
    disclosure.click();
    primary("Home").click();
    if (mode === "passive") {
      const invoke = window.__TAURI_INTERNALS__?.invoke;
      const rejects = async (command, params, message) => {
        try {
          await invoke(command, params);
          return false;
        } catch (error) {
          return message.test(String(error));
        }
      };
      const summary = { method: "project.summary", params: { root: projectRoot } };
      const traversal = { method: "project.summary", params: { root: projectRoot + "\\..\\outside" } };
      if (!invoke || window.__TAURI__ || !["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"].includes(
        location.protocol + "//" + location.hostname,
      ) || !(await rejects("plugin:window|title", { label: "main" }, /not allowed|denied/i)) ||
        !(await rejects("workspace_request", { method: "corinthos.unlisted", params: {} }, /workspace operation is unavailable/)) ||
        !(await rejects("workspace_request", traversal, /absolute local project directory/))) {
        return report("fail [ipc-policy]");
      }
      const deadline = performance.now() + 5_000;
      let after;
      while (performance.now() < deadline) {
        try {
          after = await invoke("workspace_request", summary);
          break;
        } catch (error) {
          // The observer bypasses the frontend's queue while Home reads the registry.
          if (String(error) !== "The sidecar is busy. Retry the request.") return report("fail [ipc-summary]");
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
      }
      if (!after?.initialized || typeof after.root !== "string" ||
        normalizeWindowsPath(after.root) !== normalizeWindowsPath(projectRoot)) return report("fail [ipc-summary]");
    }
    if (mode.startsWith("command-")) return verifyCommand();
    if (mode.startsWith("import-")) return verifyImport();
    if (mode === "adaptive" || mode === "adaptive-passive") return verifyAdaptive();
    const startSessions = mode === "sessions";

    const projectRow = () => Array.from(document.querySelectorAll(".project-list li")).find((row) => {
      const path = text(row.querySelector("code"));
      return path && normalizeWindowsPath(path) === normalizeWindowsPath(projectRoot);
    });
    const currentRegistry = await waitFor(() => {
      const missing = Array.from(document.querySelectorAll(".project-list li")).find(
        (row) => text(row.querySelector("h3")) === "missing-project",
      );
      return text(projectRow()?.querySelector(".section-heading span")) === "Current · v9" &&
        text(missing?.querySelector(".section-heading span")) === "Missing" &&
        projectRow()?.querySelector("button")?.matches(":enabled");
    });
    if (!currentRegistry) return report("fail [registry]");
    projectRow().querySelector("button").click();

    const projectShown = await waitFor(() => {
      const project = document.querySelector(".project-result");
      const renderedPath = text(project?.querySelector("code"));
      return (
        renderedPath &&
        normalizeWindowsPath(renderedPath) === normalizeWindowsPath(projectRoot) &&
        valueFor(project, "Path") === "Directory" &&
        valueFor(project, "Initialized") === "Yes" &&
        valueFor(project, "Storage") === "Current · v9" &&
        valueFor(project, "Work Items") === "1"
      );
    });
    if (!projectShown) return report("fail [project]");

    const contextButton = view("Context");
    if (!contextButton) return report("fail [context]");
    contextButton.click();
    const contextReady = await waitFor(() => {
      const work = document.querySelector("#context-work-items option");
      const filter = document.querySelector("#context-work");
      return text(work) === "TASK · ACTIVE" && filter && !filter.matches(":disabled");
    });
    if (!contextReady) return report("fail [context]");
    const workId = document.querySelector("#context-work-items option").value;
    setInput(document.querySelector("#context-work"), workId);
    setInput(document.querySelector("#context-provider"), "codex");
    if (!(await waitFor(() => buttonNamed(".context-audit button", "Create projection")?.matches(":enabled")))) {
      return report("fail [context]");
    }
    const create = buttonNamed(".context-audit button", "Create projection");
    create.form.requestSubmit(create);
    const projection = () => document.querySelector(".projection-detail");
    const projectionReady = await waitFor(() => {
      const sources = Array.from(document.querySelectorAll(".context-sources li"));
      return valueFor(projection(), "Work Item") === workId &&
        valueFor(projection(), "Target provider") === "codex" &&
        valueFor(projection(), "Selector policy") === "1" &&
        valueFor(projection(), "Redaction policy") === "1" &&
        sha256(valueFor(projection(), "Projection digest")) &&
        sha256(valueFor(projection(), "Source set digest")) &&
        sources.some((source) =>
          text(source.querySelector("strong")) === "WORK_ITEM · OPERATIONAL" &&
          valueFor(source, "Identity") === workId &&
          sha256(valueFor(source, "Digest")) &&
          !["", "None applied", undefined].includes(valueFor(source, "Redaction")),
        );
    });
    if (!projectionReady || revealsSecret(".context-audit")) return report("fail [context]");
    const verifyButton = buttonNamed(".context-audit button", "Verify projection");
    if (!verifyButton) return report("fail [verify]");
    verifyButton.click();
    if (!(await waitFor(() => text(projection()?.querySelector("[role=status]")) === "Verified"))) {
      return report("fail [verify]");
    }

    if (!view("Agents")) return report("fail [agents]");
    view("Agents").click();
    const passive = await waitFor(() => {
      const work = document.querySelector("#session-work-items option");
      return text(document.querySelector(".agents .session-list"))?.includes("SUCCEEDED") &&
        text(work) === "Preserve this Work Item";
    });
    if (!passive) return report("fail [agents]");
    const existingSession = document.querySelector(".session-list li[data-state=SUCCEEDED] button");
    if (!existingSession?.matches(":enabled")) return report("fail [binding]");
    existingSession.click();
    if (!(await waitFor(() => valueFor(document.querySelector(".session-detail"), "State") === "SUCCEEDED")) ||
        !(await waitFor(() => sha256(valueFor(document.querySelector(".session-projection"), "Context digest"))))) {
      return report("fail [binding]");
    }
    if (!startSessions) {
      const passiveTarget = document.querySelector("#switch-target-provider");
      if (!passiveTarget) return report("fail [switch]");
      setInput(passiveTarget, "claude");
      if (!(await waitFor(() => text(document.querySelector(".switch-stage")) === "SOURCE_TERMINAL"))) {
        return report("fail [switch]");
      }
      return report("pass");
    }

    const details = () => document.querySelector(".session-detail");
    const state = () => valueFor(details(), "State");
    const startButton = () => document.querySelector(".agents .work-form button[type=submit]");
    const event = (kind) => Array.from(document.querySelectorAll(".session-events li strong")).some(
      (node) => text(node) === kind,
    );

    async function start(timeout) {
      const work = document.querySelector("#session-work-items option");
      setInput(document.querySelector("#session-work"), work.value);
      setInput(document.querySelector("#session-provider"), "codex");
      setInput(document.querySelector("#session-timeout"), String(timeout));
      setInput(document.querySelector("#session-instruction"), "Keep the project unchanged.");
      if (!(await waitFor(() => startButton() && !startButton().disabled))) return false;
      const previous = text(details()?.querySelector("h4"));
      startButton().form.requestSubmit(startButton());
      return waitFor(() => {
        const id = text(details()?.querySelector("h4"));
        return id && id !== previous && state() === "RUNNING" &&
          text(details()?.querySelector(".session-events"))?.includes("Deterministic session output") &&
          Boolean(valueFor(details(), "Context projection")) &&
          sha256(valueFor(details(), "Context digest"));
      });
    }

    if (!(await start(90))) return report("fail [start]");
    const cancel = Array.from(details().querySelectorAll("button")).find(
      (button) => text(button) === "Cancel session",
    );
    if (!cancel) return report("fail [cancel]");
    cancel.click();
    if (!(await waitFor(() => state() === "CANCELLED" && event("CANCELLED")))) {
      return report("fail [cancel]");
    }
    const cancelledSession = text(details()?.querySelector("h4"));
    view("Context").click();
    if (!(await waitFor(() => document.querySelector('#handoff-session') &&
          !document.querySelector('#handoff-session').matches(':disabled')))) return report("fail [handoff]");
    setInput(document.querySelector("#handoff-session"), cancelledSession);
    if (!(await waitFor(() => buttonNamed(".context-audit button", "Create handoff")?.matches(":enabled")))) {
      return report("fail [handoff]");
    }
    const createHandoff = buttonNamed(".context-audit button", "Create handoff");
    createHandoff.form.requestSubmit(createHandoff);
    const handoff = () => document.querySelector(".handoff-detail");
    if (!(await waitFor(() =>
      valueFor(handoff(), "Source session") === cancelledSession &&
      valueFor(handoff(), "Terminal state") === "CANCELLED" &&
      valueFor(handoff(), "Authority") === "DERIVED" &&
      Number(valueFor(handoff(), "Event cutoff")) > 0 &&
      sha256(valueFor(handoff(), "Handoff digest")),
    )) || revealsSecret(".context-audit")) return report("fail [handoff]");
    view("Agents").click();
    if (!(await waitFor(() => document.querySelector("#session-work-items option") &&
          !document.querySelector("#session-work").matches(":disabled")))) return report("fail [agents]");
    if (!(await start(8))) return report("fail [start]");
    if (!(await waitFor(() => state() === "TIMED_OUT" && event("TIMED_OUT")))) {
      return report("fail [timeout]");
    }
    if (!(await start(90))) return report("fail [start]");
    const sourceId = text(details()?.querySelector("h4"));
    const switchPanel = () => document.querySelector(".provider-switch");
    const switchStage = () => text(switchPanel()?.querySelector(".switch-stage"));
    const switchButton = (label) => buttonNamed(".provider-switch button", label);
    const target = switchPanel()?.querySelector("#switch-target-provider");
    if (!target) return report("fail [switch]");
    setInput(target, "claude");
    if (!(await waitFor(() => switchStage() === "SOURCE_ACTIVE" &&
          switchButton("Switch provider")?.matches(":enabled")))) return report("fail [switch]");
    switchButton("Switch provider").click();
    if (!(await waitFor(() => switchStage() === "SOURCE_TERMINAL" &&
          valueFor(switchPanel(), "Source state") === "CANCELLED" &&
          switchButton("Continue switch")?.matches(":enabled")))) return report("fail [switch]");
    switchButton("Continue switch").click();

    const targetSession = () => valueFor(switchPanel(), "Target session");
    const targetReady = await waitFor(() => {
      const id = targetSession();
      return switchStage() === "TARGET_SESSION_TERMINAL" && Boolean(id) && id !== sourceId &&
        valueFor(switchPanel(), "Source provider") === "codex" &&
        valueFor(switchPanel(), "Work Item") === workId &&
        valueFor(switchPanel(), "Target state") === "SUCCEEDED" &&
        Boolean(valueFor(switchPanel(), "Source Handoff")) &&
        sha256(valueFor(switchPanel(), "Handoff digest")) &&
        Boolean(valueFor(switchPanel(), "Target Projection")) &&
        sha256(valueFor(switchPanel(), "Projection digest"));
    });
    if (!targetReady) return report("fail [switch]");
    const targetId = targetSession();
    const targetProjection = valueFor(switchPanel(), "Target Projection");
    const targetDigest = valueFor(switchPanel(), "Projection digest");
    const replay = switchButton("Replay switch");
    replay.click();
    if (!(await waitFor(() => targetSession() === targetId &&
          text(switchPanel()?.querySelector(".switch-replay-confirmed")) === "Existing target session confirmed." &&
          switchButton("Open target session")?.matches(":enabled")))) return report("fail [replay]");
    switchButton("Open target session").click();
    if (!(await waitFor(() => text(details()?.querySelector("h4")) === targetId &&
          state() === "SUCCEEDED" && valueFor(details(), "Provider") === "claude" &&
          valueFor(details(), "Work Item") === workId))) {
      return report("fail [target-selection]");
    }
    if (!(await waitFor(() => valueFor(details(), "Context projection") === targetProjection &&
          valueFor(details(), "Context digest") === targetDigest))) {
      return report("fail [target-context]");
    }
    if (!(await waitFor(() => text(details()?.querySelector(".session-events"))?.includes(
          "Verified switch continuity from the source Handoff")))) {
      return report("fail [target-output]");
    }
    if (!(await start(90))) return report("fail [start]");
    report("pass");
  }

  void verify().catch(() => report("fail [unexpected]"));
})
