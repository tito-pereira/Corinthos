// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  bridge,
  ProjectCreationError,
  type BackendSnapshot,
  type ProjectEntry,
  type ProjectStatus,
  type ProjectSummary,
  type ProjectTab,
  type ProjectTarget,
  type ProviderStatus,
} from "./bridge";
import { Agents } from "./Agents";
import { Command } from "./Command";
import { ContextAudit } from "./ContextAudit";
import { DirectorySelection } from "./DirectorySelection";
import { ProjectOverview, ProjectValidation } from "./ProjectDetail";
import { RepositoryImport } from "./RepositoryImport";
import { Providers, ProviderSettings } from "./ProviderSettings";
import { WorkItems } from "./WorkItems";

type Screen = "Home" | "Command" | "Projects" | "Settings";
const screens: Screen[] = ["Home", "Command", "Projects", "Settings"];

type CreationState = {
  name: string;
  root: string;
  shown: boolean;
  pending: boolean;
  outcome: "draft" | "uncertain" | "failed" | "created";
  error: string;
  summary: ProjectSummary | null;
};

const storageLabels: Record<ProjectStatus["storage"], string> = {
  ABSENT: "No database",
  CURRENT: "Current",
  LEGACY: "Older schema",
  INVALID: "Invalid",
  UNVERIFIED: "Unverified",
};

function storageText(project: Pick<ProjectStatus, "storage" | "schema_version">) {
  const version = project.schema_version === null ? "" : ` · v${project.schema_version}`;
  return storageLabels[project.storage] + version;
}

function ProjectFacts({ project }: { project: ProjectSummary }) {
  return (
    <div className="project-result" aria-live="polite">
      <h3>{project.display_name}</h3>
      <code>{project.root}</code>
      <dl className="facts">
        <div><dt>Path</dt><dd>{project.is_directory ? "Directory" : project.exists ? "Not a directory" : "Missing"}</dd></div>
        <div><dt>Initialized</dt><dd>{project.initialized ? "Yes" : "No"}</dd></div>
        <div><dt>Storage</dt><dd>{storageText(project)}</dd></div>
        {project.active_project && (
          <div><dt>Project Contract</dt><dd>{project.active_project.contract_id} · {project.active_project.revision_id}</dd></div>
        )}
        {project.active_workflow && (
          <div><dt>Workflow</dt><dd>{project.active_workflow.contract_id} · {project.active_workflow.revision_id}</dd></div>
        )}
        {project.work_items && (
          <div><dt>Work Items</dt><dd>{project.work_items.total}</dd></div>
        )}
      </dl>
      {project.work_items && project.work_items.total > 0 && (
        <dl className="facts">
          {Object.entries(project.work_items.by_status).filter(([, count]) => count > 0).map(([status, count]) => (
            <div key={status}><dt>{status}</dt><dd>{count}</dd></div>
          ))}
          {Object.entries(project.work_items.by_scope).filter(([, count]) => count > 0).map(([scope, count]) => (
            <div key={scope}><dt>{scope}</dt><dd>{count}</dd></div>
          ))}
        </dl>
      )}
    </div>
  );
}

function RegistryEntry({ entry, busy, inspect, open }: {
  entry: ProjectEntry;
  busy: boolean;
  inspect: boolean;
  open: (root: string, explicit: boolean) => void;
}) {
  const available = entry.availability === "AVAILABLE";
  const label = available ? "Open" : "Inspect";
  return (
    <li>
      <div className="section-heading">
        <h3>{entry.display_name}</h3>
        <span>{available ? storageText(entry) : entry.availability === "MISSING" ? "Missing" : "Unavailable"}</span>
      </div>
      <code>{entry.root}</code>
      {available && <p>{entry.initialized ? "Initialized" : "Uninitialized"}</p>}
      <button
        type="button"
        disabled={busy || (!available && !inspect)}
        onClick={() => open(entry.root, available)}
      >
        {label} {entry.display_name}
      </button>
    </li>
  );
}

function Workspace({ methods, providers, onProviders, creation, onCreation, onCreate }: {
  methods: string[];
  providers: ProviderStatus[];
  onProviders: (value: ProviderStatus[]) => void;
  creation: CreationState;
  onCreation: (update: (value: CreationState) => CreationState) => void;
  onCreate: () => Promise<ProjectSummary | null>;
}) {
  const [screen, setScreen] = useState<Screen>("Home");
  const [view, setView] = useState<ProjectTarget>({ tab: "overview" });
  const root = creation.root;
  const setRoot = (value: string) => onCreation((previous) => ({ ...previous, root: value }));
  const [project, setProject] = useState<ProjectSummary | null>(null);
  const [selectedWork, setSelectedWork] = useState<Record<string, string>>({});
  const [registry, setRegistry] = useState<ProjectEntry[]>([]);
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [listError, setListError] = useState("");
  const [loading, setLoading] = useState(true);
  const [opening, setOpening] = useState(false);
  const [pickerPending, setPickerPending] = useState(false);
  const pickerBusy = useRef(false);
  const [openError, setOpenError] = useState("");
  const [creationFields, setCreationFields] = useState({ name: "", root: "" });
  const [showPlanning, setShowPlanning] = useState(false);
  const [commandVersion, setCommandVersion] = useState(0);
  const openRequest = useRef(0);
  const navigation = useRef(0);
  const mounted = useRef(true);
  const canList = methods.includes("projects.list");
  const canOpen = methods.includes("project.open");
  const canCreate = methods.includes("project.create");
  const canInspect = methods.includes("project.summary");
  const canViewAgents = Boolean(project?.initialized && (project.schema_version ?? 0) >= 8 && methods.includes("session.list"));
  const canAuditContext = Boolean(project?.initialized && (project.schema_version ?? 0) >= 9 && methods.includes("context.list"));
  const canViewWork = Boolean(project?.initialized &&
    (project.storage === "CURRENT" || project.storage === "ABSENT") && methods.includes("work.tree"));
  const canViewValidation = Boolean(project?.initialized && methods.includes("command.project"));
  const listRequest = useRef(0);

  function pickingChanged(pending: boolean) {
    pickerBusy.current = pending;
    setPickerPending(pending);
  }

  async function loadProjects(target: number) {
    if (!canList) return;
    const current = ++listRequest.current;
    setLoading(true);
    setListError("");
    try {
      const result = await bridge.projects(target);
      if (listRequest.current !== current) return;
      setRegistry(result.projects);
      setOffset(target);
      setNextOffset(result.next_offset);
    } catch {
      if (listRequest.current === current) setListError("Could not load projects.");
    } finally {
      if (listRequest.current === current) setLoading(false);
    }
  }

  useEffect(() => {
    void loadProjects(0);
    return () => { listRequest.current += 1; };
  }, [canList]);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; navigation.current += 1; };
  }, []);

  function changeScreen(value: Screen) {
    navigation.current += 1;
    setScreen(value);
  }

  async function openProject(path: string, explicit: boolean, target: ProjectTarget = { tab: "overview" }) {
    if (pickerBusy.current || !path.trim() || (explicit ? !canOpen : !canInspect)) return;
    const request = ++openRequest.current;
    navigation.current += 1;
    setOpening(true);
    setOpenError("");
    setProject(null);
    setView(target);
    setScreen("Projects");
    try {
      const result = await bridge.project(path, explicit);
      if (explicit) await loadProjects(0);
      if (request === openRequest.current) setProject(result);
    } catch {
      if (request === openRequest.current) setOpenError(explicit
        ? "Could not open this project. Check its private state or inspect the saved entry."
        : "Could not inspect this project. Retry from Projects or Command.");
    } finally {
      if (request === openRequest.current) setOpening(false);
    }
  }

  function navigate(path: string, target: ProjectTarget) {
    if (target.tab === "work" && target.id) {
      const workId = target.id;
      setSelectedWork((previous) => ({ ...previous, [path]: workId }));
    }
    if (project?.root === path) {
      navigation.current += 1;
      setView(target);
      setScreen("Projects");
    } else {
      void openProject(path, false, target);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pickerBusy.current) return;
    if (!creation.shown) {
      void openProject(root, true);
      return;
    }
    if (creation.pending || opening || !canCreate) return;
    const fields = {
      name: !creation.name.trim() ? "Enter a project name." :
        Array.from(creation.name).length > 128 || /\p{C}/u.test(creation.name)
          ? "Use at most 128 characters without control or unsupported characters." : "",
      root: !root.trim() ? "Enter the project directory." :
        !/^(?:[A-Za-z]:[\\/]|\/(?!\/))/.test(root) || /[\0\r\n]/.test(root)
          ? "Enter an absolute local directory path." : "",
    };
    setCreationFields(fields);
    if (fields.name || fields.root) return;
    const current = navigation.current;
    void onCreate().then(async (result) => {
      if (!result || !mounted.current) return;
      setCommandVersion((value) => value + 1);
      if (current === navigation.current) {
        setProject(result);
        setView({ tab: "overview" });
        setShowPlanning(false);
        setScreen("Projects");
      }
      await loadProjects(0);
    });
  }

  const handlesProjects = screen === "Home" || screen === "Projects";
  const tabs: { tab: ProjectTab; label: string; enabled: boolean }[] = [
    { tab: "overview", label: "Overview", enabled: true },
    { tab: "work", label: "Work", enabled: canViewWork },
    { tab: "agents", label: "Agents", enabled: canViewAgents },
    { tab: "context", label: "Context", enabled: canAuditContext },
    { tab: "validation", label: "Validation", enabled: canViewValidation },
  ];
  const activeTab = tabs.some((tab) => tab.enabled && view.tab === tab.tab) ? view.tab : "overview";
  const activeWorkId = project ? view.id ?? selectedWork[project.root] : undefined;

  return (
    <>
      <nav className="primary-navigation" aria-label="Primary navigation">
        {screens.map((name) => <button type="button" key={name} aria-current={screen === name ? "page" : undefined}
          onClick={() => changeScreen(name)}>{name}</button>)}
      </nav>
      {screen === "Command" && <Command key={commandVersion} methods={methods} open={navigate} />}
      {screen === "Settings" && <ProviderSettings methods={methods} root={project?.root}
        providers={providers} onProviders={onProviders} />}
      {handlesProjects && <section aria-labelledby="projects-title">
        <div className="section-heading">
          <h2 id="projects-title">{screen === "Home" ? "Recent projects" : "Projects"}</h2>
          {screen === "Home" && <button type="button" className="secondary" onClick={() => changeScreen("Command")}>Open Command</button>}
        </div>
        {!canList && <p>Project browsing is unavailable in this backend.</p>}
        {loading && canList && <p role="status">Loading projects…</p>}
        {listError && <p role="alert">{listError} <button disabled={opening} onClick={() => void loadProjects(offset)}>Retry</button></p>}
        {canList && !loading && !listError && registry.length === 0 && <p>No projects are recorded.</p>}
        <ul className="project-list">
          {(screen === "Home" ? registry.slice(0, 5) : registry).map((entry) => (
            <RegistryEntry
              key={entry.root}
              entry={entry}
              busy={opening || creation.pending || pickerPending || (entry.availability === "AVAILABLE" && !canOpen)}
              inspect={canInspect}
              open={(path, explicit) => void openProject(path, explicit)}
            />
          ))}
        </ul>
        {screen === "Projects" && <div className="field-row">
          {offset > 0 && <button disabled={loading || opening} onClick={() => void loadProjects(0)}>First projects</button>}
          {nextOffset !== null && (
            <button disabled={loading || opening} onClick={() => void loadProjects(nextOffset)}>More projects</button>
          )}
        </div>}
        {screen === "Home" && <button type="button" disabled={!canCreate || opening || creation.pending || pickerPending}
          onClick={() => {
            setCreationFields({ name: "", root: "" });
            onCreation((previous) => ({ ...previous, shown: true }));
          }}>Create new project</button>}
        <form onSubmit={submit} aria-label={creation.shown ? "Create project" : "Open project"} noValidate={creation.shown}>
          {creation.shown && <>
            <h3>Create new project</h3>
            <p>The name is application metadata. Creation preserves existing files and prepares only private Corinthos state.</p>
            <label htmlFor="project-name">Project name</label>
            <input id="project-name" value={creation.name} maxLength={256} required disabled={creation.pending || pickerPending}
              aria-invalid={Boolean(creationFields.name)} aria-describedby={creationFields.name ? "project-name-error" : undefined}
              onChange={(event) => {
                const name = event.target.value;
                onCreation((previous) => ({ ...previous, name }));
                setCreationFields((previous) => ({ ...previous, name: "" }));
              }} />
            {creationFields.name && <p id="project-name-error" role="alert">{creationFields.name}</p>}
          </>}
          <DirectorySelection id="project-root" label="Project root" value={root}
            context={`${screen}:${creation.shown}:${creation.name}`}
            disabled={(creation.shown ? !canCreate : !canOpen) || opening || creation.pending || pickerPending}
            invalid={Boolean(creation.shown && creationFields.root)}
            describedBy={creation.shown && creationFields.root ? "project-root-error" : undefined}
            onPickingChange={pickingChanged} onChange={(path) => {
              setRoot(path);
              setCreationFields((previous) => ({ ...previous, root: "" }));
            }} />
          <div className="field-row">
            <button type="submit" disabled={(creation.shown ? !canCreate : !canOpen) || opening || creation.pending || pickerPending || (!creation.shown && !root.trim())}>
              {creation.shown ? creation.outcome === "uncertain" || creation.outcome === "failed" ? "Retry creation" : "Create project" : "Open project"}
            </button>
            {creation.shown && <button type="button" className="secondary" onClick={() => {
              navigation.current += 1;
              onCreation((previous) => ({ ...previous, shown: false }));
              setCreationFields({ name: "", root: "" });
            }}>{creation.pending ? "Leave creation" : "Cancel creation"}</button>}
          </div>
          {creation.shown && creationFields.root && <p id="project-root-error" role="alert">{creationFields.root}</p>}
        </form>
        {creation.outcome === "created" && creation.summary && <p role="status">
          Created {creation.summary.display_name}. <button type="button" disabled={opening || creation.pending}
            onClick={() => void openProject(creation.summary!.root, false)}>Inspect created project</button>
        </p>}
        {opening && <p role="status">Opening project…</p>}
        {openError && <p role="alert">{openError}</p>}
        {methods.includes("import.inspect") && <RepositoryImport
          canCommit={methods.includes("import.commit")} canCheckStatus={methods.includes("import.status")}
          canOpen={canOpen} disabled={opening || creation.pending || pickerPending}
          selectionContext={`${screen}:${creation.shown}`} onPickingChange={pickingChanged}
          onOpen={async (importedRoot) => {
            if (canOpen) await openProject(importedRoot, true);
            else await loadProjects(0);
          }} />}
      </section>}
      {screen === "Home" && <section aria-labelledby="provider-title">
        <div className="section-heading"><h2 id="provider-title">Providers</h2><span>Cached status</span></div>
        <Providers providers={providers} />
      </section>}
      {screen === "Projects" && project && (
        <section aria-labelledby="project-title">
          <div className="section-heading"><h2 id="project-title">Project Summary</h2></div>
          <ProjectFacts project={project} />
          {project.initialized && !project.active_project && <div>
            <button type="button" className="secondary" onClick={() => setShowPlanning((value) => !value)}>
              Continue with Project Manager
            </button>
            {showPlanning && <div>
              <p>Project Manager draft authoring is available through the terminal. GUI authoring is unavailable in this version.</p>
              <p>In <code>{project.root}</code>, run <code>corinthos project --root . start</code>.</p>
              <p>Reserved decisions still require the trusted foreground terminal approval.</p>
            </div>}
          </div>}
          <nav className="field-row project-views" aria-label="Project views">
            {tabs.filter((tab) => tab.enabled).map(({ tab, label }) => <button type="button" key={tab}
              aria-pressed={activeTab === tab} onClick={() => setView({ tab })}>{label}</button>)}
          </nav>
          {activeTab === "overview" && <ProjectOverview key={project.root}
            root={project.root} methods={methods} target={view} open={navigate} />}
          {activeTab === "agents" && (
            <Agents key={project.root + (view.id ?? "")} root={project.root} methods={methods} providers={providers}
              initialId={view.id} openTarget={(target) => navigate(project.root, target)} />
          )}
          {activeTab === "context" && (
            <ContextAudit key={project.root + (view.id ?? "")} root={project.root} methods={methods} providers={providers}
              initialId={view.id} initialHandoff={view.handoff} openTarget={(target) => navigate(project.root, target)} />
          )}
          {activeTab === "validation" && <ProjectValidation key={project.root}
            root={project.root} methods={methods} target={view} open={navigate} />}

          {activeTab === "work" && (
            <WorkItems
              key={project.root + (view.id ?? "")}
              root={project.root}
              methods={methods}
              providers={providers}
              initialId={activeWorkId}
              onSelect={(id) => setSelectedWork((previous) => ({ ...previous, [project.root]: id }))}
              openTarget={(target) => navigate(project.root, target)}
              onChange={async () => {
                if (canInspect) {
                  const result = await bridge.project(project.root, false);
                  setProject((current) => current?.root === result.root ? result : current);
                }
              }}
            />
          )}
        </section>
      )}
    </>
  );
}

export function App() {
  const [snapshot, setSnapshot] = useState<BackendSnapshot | null>(null);
  const request = useRef(0);
  const creationRequest = useRef(0);
  const creationInFlight = useRef(false);
  const [creation, setCreation] = useState<CreationState>({
    name: "", root: "", shown: false, pending: false, outcome: "draft", error: "", summary: null,
  });

  function disconnected(error = "The sidecar connection closed.") {
    request.current += 1;
    creationRequest.current += 1;
    creationInFlight.current = false;
    setCreation((previous) => previous.pending ? { ...previous, pending: false, shown: true, outcome: "uncertain",
      error: "Creation may have completed before disconnect. Reconnect, inspect the selected directory, or explicitly retry with the same name and path." } : previous);
    setSnapshot({ connection: "disconnected", error });
  }

  async function createProject() {
    if (creationInFlight.current) return null;
    creationInFlight.current = true;
    const current = ++creationRequest.current;
    setCreation((previous) => ({ ...previous, pending: true, outcome: "draft", error: "", summary: null }));
    try {
      const result = await bridge.createProject(creation.root, creation.name);
      if (current !== creationRequest.current) return null;
      setCreation((previous) => ({ ...previous, pending: false, shown: false, outcome: "created", summary: result }));
      return result;
    } catch (error) {
      const code = error instanceof ProjectCreationError ? error.creationCode : "UNCONFIRMED";
      const messages = {
        INVALID_INPUT: "Check the project name and absolute local directory path, then retry creation.",
        CONFLICT: "This directory already has a different creation name or conflicting state. Inspect it and retry only with its original name and path.",
        ACCESS_DENIED: "Corinthos could not access the selected directory. Check its permissions, then retry with the same name and path.",
        STATE_UNAVAILABLE: "Corinthos could not verify the existing private state. Its contents were retained; inspect the selected directory before retrying.",
        REGISTRY_INCOMPLETE: "Private initialization completed, but Projects could not save the entry. Retry with the same name and path to recover registration.",
        UNCONFIRMED: "Creation was not confirmed. Inspect the selected directory or explicitly retry with the same name and path; valid initialized state is retained.",
      };
      if (current === creationRequest.current) setCreation((previous) => ({ ...previous, pending: false,
        outcome: code === "UNCONFIRMED" ? "uncertain" : "failed", error: messages[code] }));
      return null;
    } finally {
      if (current === creationRequest.current) creationInFlight.current = false;
    }
  }

  useEffect(() => {
    let mounted = true;
    let unlisten: (() => void) | undefined;
    const current = ++request.current;
    async function connect() {
      try {
        const release = await bridge.onDisconnect(() => {
          if (mounted) disconnected();
        });
        if (!mounted) {
          release();
          return;
        }
        unlisten = release;
        const result = await bridge.snapshot();
        if (mounted && request.current === current) setSnapshot(result);
      } catch {
        if (mounted && request.current === current) disconnected("The desktop bridge could not connect.");
      }
    }
    void connect();
    return () => {
      mounted = false;
      request.current += 1;
      creationRequest.current += 1;
      unlisten?.();
    };
  }, []);

  async function reconnect() {
    const current = ++request.current;
    setSnapshot(null);
    try {
      const result = await bridge.restart();
      if (request.current === current) setSnapshot(result);
    } catch {
      if (request.current === current) disconnected("The sidecar could not restart.");
    }
  }

  const ready = snapshot?.connection === "ready";
  const connection = snapshot?.connection ?? "connecting";

  return (
    <main>
      <header className="masthead">
        <div>
          <h1>Corinthos</h1>
          <p>Local project operations</p>
        </div>
        <span className={`connection ${connection}`} role="status">
          {ready ? "Connected" : snapshot ? "Disconnected" : "Connecting…"}
        </span>
      </header>

      <section aria-labelledby="backend-title">
        <div className="section-heading"><h2 id="backend-title">Backend</h2></div>
        {snapshot === null && <p>Waiting for the sidecar handshake.</p>}
        {snapshot?.connection === "disconnected" && (
          <div className="failure">
            <p role="alert">{snapshot.error ?? "The sidecar is disconnected."}</p>
            <button type="button" onClick={() => void reconnect()}>Reconnect</button>
          </div>
        )}
        {ready && (
          <dl className="facts">
            <div><dt>Application version</dt><dd>{snapshot.handshake.version}</dd></div>
            <div><dt>RPC protocol</dt><dd>v{snapshot.handshake.protocol_version}</dd></div>
          </dl>
        )}
      </section>

      {creation.pending && <p role="status">Creating project. The outcome is not yet confirmed. Leaving this form does not cancel backend initialization.</p>}
      {creation.error && <div role="alert"><p>{creation.error}</p>
        <p>{creation.name} <code>{creation.root}</code></p>
      </div>}
      {ready && <Workspace methods={snapshot.handshake.methods} providers={snapshot.providers.providers}
        creation={creation} onCreation={setCreation} onCreate={createProject}
        onProviders={(providers) => setSnapshot((current) => current?.connection === "ready"
          ? { ...current, providers: { providers } } : current)} />}
    </main>
  );
}
