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
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), "value").set;
    setter.call(field, value);
    field.dispatchEvent(new Event(field instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  };

  const buttonNamed = (selector, label) => Array.from(document.querySelectorAll(selector)).find(
    (button) => text(button) === label,
  );
  const view = (name) => buttonNamed(".project-views button", name);
  const sha256 = (value) => /^[a-f0-9]{64}$/.test(value ?? "");
  const revealsSecret = (selector) => (text(document.querySelector(selector)) ?? "").includes(
    "v8-fixture-hidden-value",
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
      const form = document.querySelector("#import-root");
      if (!form?.matches(":enabled")) return report("fail [import-preview]");
      setInput(form, projectRoot);
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
        valueFor(project(), "Project Contract") === undefined && valueFor(project(), "Workflow") === undefined &&
        document.querySelector("#work-objective")?.matches(":enabled");
    }))) return report("fail [import-project]");
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
    const projectForm = document.querySelector("#project-root");
    setInput(projectForm, projectRoot);
    const openProject = () => buttonNamed("form button", "Open project");
    if (!(await waitFor(() => openProject()?.matches(":enabled")))) return report("fail [adaptive-open-form]");
    openProject().form.requestSubmit(openProject());
    if (!(await waitFor(() => {
      const path = text(summary()?.querySelector("code"));
      return path && normalizeWindowsPath(path) === normalizeWindowsPath(projectRoot);
    }))) return report("fail [adaptive-open-project]");
    if (valueFor(summary(), "Storage") !== "Current · v9") return report("fail [adaptive-storage]");
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

  async function verify() {
    const backendReady = await waitFor(() => {
      const backend = document.querySelector("#backend-title")?.closest("section");
      const codex = Array.from(document.querySelectorAll(".providers li")).find(
        (row) => text(row.querySelector("h3")) === "codex",
      );
      const configuredPath = text(codex?.querySelector("code"));
      return (
        text(document.querySelector(".connection[role=status]")) === "Connected" &&
        valueFor(backend, "Application version") === "0.1.0" &&
        valueFor(backend, "RPC protocol") === "v2" &&
        text(codex?.querySelector(".provider-heading span")) === "Not yet observed" &&
        text(codex?.querySelector("p")) === "User setting" &&
        configuredPath &&
        normalizeWindowsPath(configuredPath).endsWith("\\configured\\codex.exe")
      );
    });
    if (!backendReady) return report("fail [backend]");
    if (mode.startsWith("import-")) return verifyImport();
    if (mode === "adaptive" || mode === "adaptive-passive") return verifyAdaptive();
    const startSessions = mode === "sessions";

    const projectRow = () => Array.from(document.querySelectorAll(".project-list li")).find((row) => {
      const path = text(row.querySelector("code"));
      return path && normalizeWindowsPath(path) === normalizeWindowsPath(projectRoot);
    });
    const legacyRegistry = await waitFor(() => {
      const missing = Array.from(document.querySelectorAll(".project-list li")).find(
        (row) => text(row.querySelector("h3")) === "missing-project",
      );
      return text(projectRow()?.querySelector(".section-heading span")) === (startSessions ? "Current · v9" : "Older schema · v8") &&
        text(missing?.querySelector(".section-heading span")) === "Missing";
    });
    if (!legacyRegistry) return report("fail [registry]");
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
    const legacySession = document.querySelector(".session-list li[data-state=SUCCEEDED] button");
    legacySession.click();
    if (!(await waitFor(() => valueFor(document.querySelector(".session-detail"), "State") === "SUCCEEDED")) ||
        document.querySelector(".session-projection")) return report("fail [binding]");
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
