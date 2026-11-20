// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState, type Dispatch, type FormEvent, type ReactNode, type SetStateAction } from "react";
import {
  bridge,
  ProjectCreationError,
  type BackendSnapshot,
  type ProjectEntry,
  type ProjectInspection,
  type ProjectStatus,
  type ProjectSummary,
  type ProjectTab,
  type ProjectTarget,
  type ProviderStatus,
  type RuntimeDiagnostics,
} from "./bridge";
import { Agents } from "./Agents";
import { BackendDiagnostics, connectionLabels, type ConnectionState } from "./BackendDiagnostics";
import corinthosLogo from "./assets/branding/corinthos-logo.svg?no-inline";
import corinthosSymbol from "./assets/branding/corinthos-symbol.svg?no-inline";
import { Command } from "./Command";
import { ContextAudit } from "./ContextAudit";
import { DirectorySelection } from "./DirectorySelection";
import { ProjectOverview, ProjectValidation } from "./ProjectDetail";
import { RepositoryImport } from "./RepositoryImport";
import { Providers, ProviderSettings } from "./ProviderSettings";
import { ProductUpdate } from "./ProductUpdate";
import { WorkItems } from "./WorkItems";

type Screen = "Home" | "Command" | "Projects" | "Settings";
type Onboarding = "create" | "open" | "import" | null;
type ImportDraft = { path: string; uncertain: boolean };
const screens: Screen[] = ["Home", "Command", "Projects", "Settings"];

function ApplicationShell({ screen, navigate, disabled, connection, onReconnect, children }: {
  screen: Screen;
  navigate: (value: Screen) => void;
  disabled: boolean;
  connection: ConnectionState;
  onReconnect: () => void;
  children: ReactNode;
}) {
  return <div className="app-shell">
    <aside className="app-sidebar" aria-label="Corinthos navigation">
      <header className="masthead">
        <h1><img className="brand-logo" src={corinthosLogo} alt="" />
          <img className="brand-symbol" src={corinthosSymbol} alt="" />
          <span className="visually-hidden">Corinthos</span></h1>
      </header>
      <nav className="primary-navigation" aria-label="Primary navigation">
        {screens.map((name) => <button type="button" key={name} aria-current={screen === name ? "page" : undefined}
          disabled={disabled} onClick={() => navigate(name)}>{name}</button>)}
      </nav>
      <div className="connection-control">
        <span className={`connection ${connection}`} role="status">{connectionLabels[connection]}</span>
        {(connection === "disconnected" || connection === "unavailable") &&
          <button type="button" onClick={onReconnect}>Reconnect</button>}
      </div>
    </aside>
    <main className="workspace-content">{children}</main>
  </div>;
}

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

function Workspace({ methods, providers, onProviders, creation, onCreation, onCreate, importDraft, onImportDraft,
  screen, setScreen, view, setView, project, setProject, selectedWork, setSelectedWork, renderDiagnostics, renderShell }: {
  methods: string[];
  providers: ProviderStatus[];
  onProviders: (value: ProviderStatus[]) => void;
  creation: CreationState;
  onCreation: (update: (value: CreationState) => CreationState) => void;
  onCreate: () => Promise<ProjectSummary | null>;
  importDraft: ImportDraft;
  onImportDraft: (update: (value: ImportDraft) => ImportDraft) => void;
  screen: Screen;
  setScreen: Dispatch<SetStateAction<Screen>>;
  view: ProjectTarget;
  setView: Dispatch<SetStateAction<ProjectTarget>>;
  project: ProjectSummary | null;
  setProject: Dispatch<SetStateAction<ProjectSummary | null>>;
  selectedWork: Record<string, string>;
  setSelectedWork: Dispatch<SetStateAction<Record<string, string>>>;
  renderDiagnostics: (projectCurrent: boolean) => ReactNode;
  renderShell: (children: ReactNode, navigate: (value: Screen) => void, disabled: boolean) => ReactNode;
}) {
  const [flow, setFlow] = useState<Onboarding>(creation.shown ? "create" : importDraft.uncertain ? "import" : null);
  const [openRoot, setOpenRoot] = useState("");
  const root = flow === "create" ? creation.root : openRoot;
  const [inspection, setInspection] = useState<ProjectInspection | null>(null);
  const [importMutating, setImportMutating] = useState(false);
  const [confirmedImport, setConfirmedImport] = useState<string | null>(null);
  const [refreshingSelection, setRefreshingSelection] = useState(Boolean(project));
  const projectRef = useRef(project);
  projectRef.current = project;
  const [registry, setRegistry] = useState<ProjectEntry[]>([]);
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [listError, setListError] = useState("");
  const [loading, setLoading] = useState(true);
  const [opening, setOpening] = useState(false);
  const openingBusy = useRef(false);
  const [pickerPending, setPickerPending] = useState(false);
  const pickerBusy = useRef(false);
  const [openError, setOpenError] = useState("");
  const [creationFields, setCreationFields] = useState({ name: "", root: "" });
  const [showPlanning, setShowPlanning] = useState(false);
  const [commandVersion, setCommandVersion] = useState(0);
  const openRequest = useRef(0);
  const selectionRequest = useRef(0);
  const navigation = useRef(0);
  const mounted = useRef(true);
  const canList = methods.includes("projects.list");
  const canOpen = methods.includes("project.inspect") && methods.includes("project.open_checked");
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

  async function refreshSelection() {
    const path = projectRef.current?.root;
    if (!path) return;
    const current = ++selectionRequest.current;
    const active = () => mounted.current && selectionRequest.current === current && projectRef.current?.root === path;
    setRefreshingSelection(true);
    setOpenError("");
    if (!canInspect) {
      setOpenError("Project details are unavailable in this backend.");
      return;
    }
    try {
      const result = await bridge.project(path, false);
      if (active()) {
        setProject(result);
        setRefreshingSelection(false);
      }
    } catch {
      if (active()) {
        setOpenError("Could not refresh the selected project. Retry when it is available.");
      }
    }
  }

  useEffect(() => {
    if (projectRef.current) void refreshSelection();
  }, [canInspect]);

  function changeScreen(value: Screen) {
    if (!mounted.current || importMutating) return;
    navigation.current += 1;
    openRequest.current += 1;
    openingBusy.current = false;
    setOpening(false);
    setInspection(null);
    setScreen(value);
  }

  function chooseFlow(value: Onboarding, path?: string) {
    if (!mounted.current || importMutating || pickerBusy.current || creation.pending) return;
    navigation.current += 1;
    openRequest.current += 1;
    openingBusy.current = false;
    setOpening(false);
    setInspection(null);
    setOpenError("");
    setConfirmedImport(null);
    setCreationFields({ name: "", root: "" });
    onCreation((previous) => ({ ...previous, shown: value === "create", ...(value === "create" && path !== undefined ? { root: path } : {}) }));
    if (value === "open" && path !== undefined) setOpenRoot(path);
    if (value === "import" && path !== undefined) onImportDraft((previous) => ({ ...previous, path }));
    setFlow(value);
  }

  async function openProject(path: string, explicit: boolean, target: ProjectTarget = { tab: "overview" }) {
    if (!mounted.current || pickerBusy.current || openingBusy.current || !path.trim() || (explicit ? !canOpen : !canInspect)) return;
    selectionRequest.current += 1;
    openingBusy.current = true;
    const request = ++openRequest.current;
    navigation.current += 1;
    const current = navigation.current;
    const active = () => mounted.current && request === openRequest.current && current === navigation.current;
    setOpening(true);
    setOpenError("");
    setInspection(null);
    setProject(null);
    setRefreshingSelection(false);
    setView(target);
    setScreen("Projects");
    try {
      let result: ProjectSummary;
      if (explicit) {
        setFlow("open");
        setOpenRoot(path);
        onCreation((previous) => ({ ...previous, shown: false }));
        const inspected = await bridge.inspectProject(path);
        if (!active()) return;
        if (inspected.status !== "CURRENT" || !inspected.inspection_digest) {
          setInspection(inspected);
          return;
        }
        result = await bridge.openInspectedProject(inspected.root, inspected.inspection_digest);
      } else result = await bridge.project(path, false);
      if (!mounted.current) return;
      if (explicit) {
        setCommandVersion((value) => value + 1);
        await loadProjects(0);
      }
      if (active()) {
        setProject(result);
        setFlow(null);
      }
    } catch {
      if (active()) setOpenError(explicit
        ? "Could not open this project. Inspect it again; its state may have changed or be unavailable."
        : "Could not inspect this project. Retry from Projects or Command.");
    } finally {
      if (active()) { openingBusy.current = false; setOpening(false); }
    }
  }

  async function migrateProject() {
    if (!mounted.current || !inspection?.inspection_digest || inspection.status !== "MIGRATION_REQUIRED" || openingBusy.current || pickerBusy.current) return;
    selectionRequest.current += 1;
    openingBusy.current = true;
    const selected = inspection;
    const request = ++openRequest.current;
    const current = navigation.current;
    const active = () => mounted.current && request === openRequest.current && current === navigation.current;
    setOpening(true);
    setOpenError("");
    setInspection(null);
    try {
      const result = await bridge.openInspectedProject(selected.root, selected.inspection_digest!, true);
      if (!mounted.current) return;
      setCommandVersion((value) => value + 1);
      await loadProjects(0);
      if (!active()) return;
      setProject(result);
      setRefreshingSelection(false);
      setFlow(null);
      setScreen("Projects");
    } catch {
      if (active()) setOpenError("Migration was not confirmed. Inspect the directory again before retrying.");
    } finally {
      if (active()) { openingBusy.current = false; setOpening(false); }
    }
  }

  function navigate(path: string, target: ProjectTarget) {
    if (!mounted.current) return;
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
    if (!mounted.current || pickerBusy.current) return;
    if (flow === "open") {
      if (opening || !canOpen) return;
      void openProject(root, true);
      return;
    }
    if (flow !== "create") return;
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
    selectionRequest.current += 1;
    const current = navigation.current;
    void onCreate().then(async (result) => {
      if (!result || !mounted.current) return;
      setCommandVersion((value) => value + 1);
      setFlow(null);
      if (current === navigation.current) {
        setProject(result);
        setRefreshingSelection(false);
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
  const activeWorkId = project ? selectedWork[project.root] ?? view.id : undefined;

  return renderShell(
    <>
      {screen === "Command" && <Command key={commandVersion} methods={methods} open={navigate} />}
      {screen === "Settings" && <>
        {renderDiagnostics(!refreshingSelection)}
        <ProductUpdate methods={methods} />
        <ProviderSettings methods={methods} root={project?.root} providers={providers} onProviders={onProviders} />
      </>}
      {handlesProjects && <section aria-labelledby="projects-title">
        <div className="section-heading">
          <h2 id="projects-title">{screen === "Home" ? "Recent projects" : "Projects"}</h2>
          {screen === "Home" && <button type="button" className="secondary" onClick={() => changeScreen("Command")}>Open Command</button>}
        </div>
        <div className="onboarding-choices" role="group" aria-label="Project onboarding">
          <div><button type="button" disabled={!canCreate || opening || creation.pending || pickerPending || importMutating}
            aria-pressed={flow === "create"} onClick={() => chooseFlow("create")}>Create new project</button>
            <p>Name a project and prepare private Corinthos state while preserving existing files.</p></div>
          <div><button type="button" disabled={!canOpen || opening || creation.pending || pickerPending || importMutating}
            aria-pressed={flow === "open"} onClick={() => chooseFlow("open")}>Open existing project</button>
            <p>Inspect a directory and open verified Corinthos state, with migration reviewed first.</p></div>
          <div><button type="button" disabled={!methods.includes("import.inspect") || opening || creation.pending || pickerPending || importMutating}
            aria-pressed={flow === "import"} onClick={() => chooseFlow("import")}>Import Git repository</button>
            <p>Review a supported local Git repository before confirming its import.</p></div>
        </div>
        {!canOpen && <p>Safe project opening is unavailable in this backend. Reconnect with a current backend.</p>}
        {(flow === "create" || flow === "open") && <form onSubmit={submit} aria-label={flow === "create" ? "Create project" : "Open project"} noValidate={flow === "create"}>
          {flow === "open" && <h3>Open existing project</h3>}
          {flow === "create" && <>
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
            context={`${screen}:${flow}:${creation.name}`}
            disabled={(flow === "create" ? !canCreate : !canOpen) || opening || creation.pending || pickerPending}
            invalid={Boolean(flow === "create" && creationFields.root)}
            describedBy={flow === "create" && creationFields.root ? "project-root-error" : undefined}
            onPickingChange={pickingChanged} onChange={(path) => {
              openRequest.current += 1;
              setInspection(null);
              setOpenError("");
              if (flow === "create") onCreation((previous) => ({ ...previous, root: path }));
              else setOpenRoot(path);
              setCreationFields((previous) => ({ ...previous, root: "" }));
            }} />
          <div className="field-row">
            <button type="submit" disabled={(flow === "create" ? !canCreate : !canOpen) || opening || creation.pending || pickerPending || (flow === "open" && !root.trim())}>
              {flow === "create" ? creation.outcome === "uncertain" || creation.outcome === "failed" ? "Retry creation" : "Create project" : "Open project"}
            </button>
            <button type="button" className="secondary" disabled={pickerPending} onClick={() => {
              if (creation.pending) {
                navigation.current += 1;
                setFlow(null);
                onCreation((previous) => ({ ...previous, shown: false }));
              } else chooseFlow(null);
            }}>{flow === "create" ? creation.pending ? "Leave creation" : "Cancel creation" : "Cancel"}</button>
          </div>
          {flow === "create" && creationFields.root && <p id="project-root-error" role="alert">{creationFields.root}</p>}
        </form>}
        {flow === "open" && inspection && <div className="onboarding-review" role="region" aria-label="Open project review" aria-live="polite">
          <h3>{inspection.status === "MIGRATION_REQUIRED" ? "Migration required" : inspection.status === "UNMANAGED" ? "Directory is not managed" : inspection.status === "INCOMPLETE_IMPORT" ? "Import recovery required" : "Project cannot be opened"}</h3>
          <code>{inspection.root}</code><p>{inspection.reason}</p>
          {inspection.status === "MIGRATION_REQUIRED" && <button type="button" disabled={opening || !inspection.inspection_digest}
            onClick={() => void migrateProject()}>Migrate and open</button>}
          {inspection.status === "UNMANAGED" && <>
            <button type="button" disabled={!canCreate || opening} onClick={() => chooseFlow("create", inspection.root)}>Create project here</button>
            <button type="button" disabled={!inspection.can_import || !methods.includes("import.inspect") || opening}
              onClick={() => chooseFlow("import", inspection.root)}>Import Git repository</button>
            {!inspection.can_import && <p>Import is unavailable because this directory is not a supported local Git repository.</p>}
          </>}
          {inspection.status === "INCOMPLETE_IMPORT" && <button type="button" disabled={!methods.includes("import.inspect") || opening}
            onClick={() => chooseFlow("import", inspection.root)}>Review import recovery</button>}
        </div>}
        {creation.outcome === "created" && creation.summary && <p role="status">
          Created {creation.summary.display_name}. <button type="button" disabled={opening || creation.pending}
            onClick={() => void openProject(creation.summary!.root, false)}>Inspect created project</button>
        </p>}
        {opening && <p role="status">Opening project…</p>}
        {refreshingSelection && !openError && <p role="status">Refreshing selected project…</p>}
        {openError && <p role="alert">{openError}</p>}
        {refreshingSelection && openError && <button type="button" disabled={!canInspect}
          onClick={() => void refreshSelection()}>Retry project refresh</button>}
        {flow === "import" && <>
          {importDraft.uncertain && !importMutating && <p role="alert">Import was not confirmed. Inspect the same repository to check its durable state before retrying.</p>}
          <RepositoryImport path={importDraft.path} onPathChange={(path) => {
            setConfirmedImport(null);
            onImportDraft((previous) => ({ ...previous, path }));
          }}
            onConfirmed={setConfirmedImport}
            canCommit={methods.includes("import.commit")} canCheckStatus={methods.includes("import.status")}
            canOpen={canOpen} disabled={opening || creation.pending || pickerPending}
            selectionContext={`${screen}:${flow}`} onPickingChange={pickingChanged}
            onMutationChange={(pending, uncertain) => {
              setImportMutating(pending);
              onImportDraft((previous) => ({ ...previous, uncertain }));
            }} onOpen={async (importedRoot) => {
              if (canOpen) await openProject(importedRoot, true);
              else await loadProjects(0);
            }} />
          <button type="button" className="secondary" disabled={importMutating || pickerPending} onClick={() => chooseFlow(null)}>Cancel import</button>
        </>}
        {confirmedImport && <p role="status" className="import-confirmed">CONFIRMED import <code>{confirmedImport}</code></p>}
        {!canList && <p>Project browsing is unavailable in this backend.</p>}
        {loading && canList && <p role="status">Loading projects…</p>}
        {listError && <p role="alert">{listError} <button disabled={opening} onClick={() => void loadProjects(offset)}>Retry</button></p>}
        {canList && !loading && !listError && registry.length === 0 && <p>No projects are recorded. Choose Create new project, Open existing project or Import Git repository above to get started.</p>}
        <ul className="project-list">
          {(screen === "Home" ? registry.slice(0, 5) : registry).map((entry) => (
            <RegistryEntry
              key={entry.root}
              entry={entry}
              busy={opening || creation.pending || pickerPending || importMutating || (entry.availability === "AVAILABLE" && !canOpen)}
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
      </section>}
      {screen === "Home" && <section aria-labelledby="provider-title">
        <div className="section-heading"><h2 id="provider-title">Providers</h2><span>Cached status</span></div>
        <Providers providers={providers} />
      </section>}
      {screen === "Projects" && project && !refreshingSelection && (
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
              onSelect={(id) => {
                if (mounted.current) setSelectedWork((previous) => ({ ...previous, [project.root]: id }));
              }}
              openTarget={(target) => navigate(project.root, target)}
              onChange={async () => {
                if (mounted.current && canInspect) {
                  const current = ++selectionRequest.current;
                  const result = await bridge.project(project.root, false);
                  if (mounted.current && selectionRequest.current === current) {
                    setProject((previous) => previous?.root === result.root ? result : previous);
                  }
                }
              }}
            />
          )}
        </section>
      )}
    </>, changeScreen, importMutating
  );
}

export function App() {
  const [importDraft, setImportDraft] = useState<ImportDraft>({ path: "", uncertain: false });
  const [snapshot, setSnapshot] = useState<BackendSnapshot | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [failure, setFailure] = useState("");
  const [screen, setScreen] = useState<Screen>("Home");
  const [view, setView] = useState<ProjectTarget>({ tab: "overview" });
  const [project, setProject] = useState<ProjectSummary | null>(null);
  const [selectedWork, setSelectedWork] = useState<Record<string, string>>({});
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [runtime, setRuntime] = useState<RuntimeDiagnostics | null>(null);
  const [diagnosticsPending, setDiagnosticsPending] = useState(false);
  const [diagnosticsError, setDiagnosticsError] = useState("");
  const request = useRef(0);
  const restartInFlight = useRef(false);
  const diagnosticsRequest = useRef(0);
  const diagnosticsInFlight = useRef(false);
  const creationRequest = useRef(0);
  const creationInFlight = useRef(false);
  const [creation, setCreation] = useState<CreationState>({
    name: "", root: "", shown: false, pending: false, outcome: "draft", error: "", summary: null,
  });

  function applySnapshot(result: BackendSnapshot) {
    setSnapshot(result);
    setConnection(result.connection === "ready" ? "connected" : result.connection);
    setFailure(result.connection === "ready" ? "" : result.error ?? "The sidecar is disconnected.");
  }

  function disconnected(error = "The sidecar connection closed.") {
    request.current += 1;
    creationRequest.current += 1;
    diagnosticsRequest.current += 1;
    creationInFlight.current = false;
    diagnosticsInFlight.current = false;
    restartInFlight.current = false;
    setDiagnosticsPending(false);
    setCreation((previous) => previous.pending ? { ...previous, pending: false, shown: true, outcome: "uncertain",
      error: "Creation may have completed before disconnect. Reconnect, inspect the selected directory, or explicitly retry with the same name and path." } : previous);
    setConnection("disconnected");
    setFailure(error);
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
        if (mounted && request.current === current) applySnapshot(result);
      } catch {
        if (mounted && request.current === current) {
          setConnection("unavailable");
          setFailure("The desktop bridge could not connect.");
        }
      }
    }
    void connect();
    return () => {
      mounted = false;
      request.current += 1;
      creationRequest.current += 1;
      diagnosticsRequest.current += 1;
      unlisten?.();
    };
  }, []);

  async function reconnect() {
    if (restartInFlight.current) return;
    restartInFlight.current = true;
    const current = ++request.current;
    diagnosticsRequest.current += 1;
    diagnosticsInFlight.current = false;
    setDiagnosticsPending(false);
    setConnection("restarting");
    setRuntime(null);
    setFailure("");
    try {
      const result = await bridge.restart();
      if (request.current === current) applySnapshot(result);
    } catch {
      if (request.current === current) disconnected("The sidecar could not restart.");
    } finally {
      if (request.current === current) restartInFlight.current = false;
    }
  }

  async function loadDiagnostics() {
    if (connection !== "connected" || snapshot?.connection !== "ready" ||
        !snapshot.handshake.methods.includes("system.diagnostics") || diagnosticsInFlight.current) return;
    diagnosticsInFlight.current = true;
    const current = ++diagnosticsRequest.current;
    const generation = request.current;
    setDiagnosticsPending(true);
    setDiagnosticsError("");
    try {
      const result = await bridge.diagnostics();
      if (diagnosticsRequest.current === current && request.current === generation) setRuntime(result);
    } catch {
      if (diagnosticsRequest.current === current && request.current === generation) {
        setDiagnosticsError("Runtime diagnostics could not be read. Close and reopen this view to retry.");
      }
    } finally {
      if (diagnosticsRequest.current === current && request.current === generation) {
        diagnosticsInFlight.current = false;
        setDiagnosticsPending(false);
      }
    }
  }

  useEffect(() => {
    if (showDiagnostics && connection === "connected") void loadDiagnostics();
  }, [connection]);

  function diagnostics(projectCurrent: boolean) {
    return <BackendDiagnostics snapshot={snapshot} connection={connection} runtime={runtime}
      project={project} projectCurrent={projectCurrent} open={showDiagnostics} pending={diagnosticsPending}
      error={diagnosticsError} onToggle={(open) => {
        setShowDiagnostics(open);
        if (open && !showDiagnostics) void loadDiagnostics();
      }} />;
  }

  const ready = connection === "connected" && snapshot?.connection === "ready";
  const generation = request.current;

  function renderShell(children: ReactNode, navigate: (value: Screen) => void, disabled: boolean) {
    return <ApplicationShell screen={screen} navigate={navigate} disabled={disabled} connection={connection}
      onReconnect={() => void reconnect()}>
      {creation.pending && <p role="status">Creating project. The outcome is not yet confirmed. Leaving this form does not cancel backend initialization.</p>}
      {creation.error && <div role="alert"><p>{creation.error}</p>
        <p>{creation.name} <code>{creation.root}</code></p>
      </div>}
      {children}
    </ApplicationShell>;
  }

  return ready ? <Workspace methods={snapshot.handshake.methods} providers={snapshot.providers.providers}
        creation={creation} onCreation={setCreation} onCreate={createProject} importDraft={importDraft} onImportDraft={setImportDraft}
        screen={screen} setScreen={setScreen} view={view} setView={setView} project={project} setProject={setProject}
        selectedWork={selectedWork} setSelectedWork={setSelectedWork} renderDiagnostics={diagnostics} renderShell={renderShell}
        onProviders={(providers) => {
          if (request.current === generation) setSnapshot((current) => current?.connection === "ready"
            ? { ...current, providers: { providers } } : current);
        }} /> : renderShell(<section aria-labelledby="support-title">
          <div className="section-heading"><h2 id="support-title">Backend support</h2></div>
          {failure && <p role="alert">{failure}</p>}
          <p>{connection === "connecting" ? "Waiting for the sidecar handshake."
            : connection === "restarting" ? "Restarting the sidecar."
            : "Check the private runtime and reconnect when it is available."}</p>
          {diagnostics(true)}
        </section>, () => {}, true);
}
