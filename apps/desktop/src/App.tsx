// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  bridge,
  type BackendSnapshot,
  type ProjectEntry,
  type ProjectStatus,
  type ProjectSummary,
  type ProviderStatus,
} from "./bridge";
import { Agents } from "./Agents";
import { ContextAudit } from "./ContextAudit";
import { RepositoryImport } from "./RepositoryImport";
import { WorkItems } from "./WorkItems";

const storageLabels: Record<ProjectStatus["storage"], string> = {
  ABSENT: "No database",
  CURRENT: "Current",
  LEGACY: "Older schema",
  INVALID: "Invalid",
  UNVERIFIED: "Unverified",
};

const configurationLabels: Record<ProviderStatus["configuration"], string> = {
  operational: "Project override",
  initial: "Initial project choice",
  global: "User setting",
  default: "Automatic selection",
};

const observationLabels: Record<ProviderStatus["observation"], string> = {
  OBSERVED: "Previously observed",
  NOT_OBSERVED: "Not yet observed",
  UNKNOWN: "Unknown",
};

function Providers({ providers }: { providers: ProviderStatus[] }) {
  return (
    <section aria-labelledby="provider-title">
      <div className="section-heading">
        <h2 id="provider-title">Providers</h2>
        <span>Cached status</span>
      </div>
      {providers.length ? (
        <ul className="providers">
          {providers.map((row) => (
            <li key={row.provider}>
              <div className="provider-heading">
                <h3>{row.provider}</h3>
                <span>{observationLabels[row.observation]}</span>
              </div>
              <p>{row.supported ? configurationLabels[row.configuration] : "Unsupported provider"}</p>
              {row.executable && <code>{row.executable}</code>}
              {row.capabilities && row.observation === "OBSERVED" && (
                <p>
                  {row.capabilities.available ? "Available at last check" : "Unavailable at last check"}
                  {row.capabilities.version && ` · ${row.capabilities.version}`}
                  {row.capabilities.json_events && " · JSON events"}
                  {row.capabilities.stdin_prompt && " · stdin"}
                  {row.capabilities.native_resume && " · Resume"}
                  {row.capabilities.read_only && " · Read-only"}
                </p>
              )}
            </li>
          ))}
        </ul>
      ) : <p>No providers are recorded.</p>}
    </section>
  );
}

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

function Projects({ methods, providers }: { methods: string[]; providers: ProviderStatus[] }) {
  const [view, setView] = useState<"work" | "agents" | "context">("work");
  const [root, setRoot] = useState("");
  const [project, setProject] = useState<ProjectSummary | null>(null);
  const [registry, setRegistry] = useState<ProjectEntry[]>([]);
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [listError, setListError] = useState("");
  const [loading, setLoading] = useState(true);
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState("");
  const canList = methods.includes("projects.list");
  const canOpen = methods.includes("project.open");
  const canInspect = methods.includes("project.summary");
  const canViewAgents = Boolean(project?.initialized && (project.schema_version ?? 0) >= 8 && methods.includes("session.list"));
  const canAuditContext = Boolean(project?.initialized && (project.schema_version ?? 0) >= 9 && methods.includes("context.list"));
  const listRequest = useRef(0);

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

  async function openProject(path: string, explicit: boolean) {
    if (opening || !path.trim() || (explicit ? !canOpen : !canInspect)) return;
    setOpening(true);
    setOpenError("");
    setProject(null);
    setView("work");
    try {
      const result = await bridge.project(path, explicit);
      if (explicit) await loadProjects(0);
      setProject(result);
    } catch {
      setOpenError(explicit ? "Could not open this project." : "Could not inspect this project.");
    } finally {
      setOpening(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void openProject(root, true);
  }

  return (
    <>
      <section aria-labelledby="projects-title">
        <div className="section-heading"><h2 id="projects-title">Projects</h2></div>
        {!canList && <p>Project browsing is unavailable in this backend.</p>}
        {loading && canList && <p role="status">Loading projects…</p>}
        {listError && <p role="alert">{listError} <button disabled={opening} onClick={() => void loadProjects(offset)}>Retry</button></p>}
        {canList && !loading && !listError && registry.length === 0 && <p>No projects are recorded.</p>}
        <ul className="project-list">
          {registry.map((entry) => (
            <RegistryEntry
              key={entry.root}
              entry={entry}
              busy={opening || (entry.availability === "AVAILABLE" && !canOpen)}
              inspect={canInspect}
              open={(path, explicit) => void openProject(path, explicit)}
            />
          ))}
        </ul>
        <div className="field-row">
          {offset > 0 && <button disabled={loading || opening} onClick={() => void loadProjects(0)}>First projects</button>}
          {nextOffset !== null && (
            <button disabled={loading || opening} onClick={() => void loadProjects(nextOffset)}>More projects</button>
          )}
        </div>
        <form onSubmit={submit}>
          <label htmlFor="project-root">Project root</label>
          <div className="field-row">
            <input
              id="project-root"
              value={root}
              onChange={(event) => setRoot(event.target.value)}
              placeholder="Enter a local directory path"
              maxLength={4096}
              disabled={!canOpen || opening}
              required
            />
            <button type="submit" disabled={!canOpen || opening || !root.trim()}>Open project</button>
          </div>
        </form>
        {opening && <p role="status">Opening project…</p>}
        {openError && <p role="alert">{openError}</p>}
        {methods.includes("import.inspect") && <RepositoryImport
          canCommit={methods.includes("import.commit")} canCheckStatus={methods.includes("import.status")}
          canOpen={canOpen} disabled={opening}
          onOpen={async (importedRoot) => {
            if (canOpen) await openProject(importedRoot, true);
            else await loadProjects(0);
          }} />}
      </section>
      {project && (
        <section aria-labelledby="project-title">
          <div className="section-heading"><h2 id="project-title">Project Summary</h2></div>
          <ProjectFacts project={project} />
          {(canViewAgents || canAuditContext) && (
            <div className="field-row project-views" aria-label="Project views">
              <button type="button" aria-pressed={view === "work"} onClick={() => setView("work")}>Work Items</button>
              {canViewAgents && <button type="button" aria-pressed={view === "agents"} onClick={() => setView("agents")}>Agents</button>}
              {canAuditContext && <button type="button" aria-pressed={view === "context"} onClick={() => setView("context")}>Context</button>}
            </div>
          )}
          {view === "agents" && canViewAgents && (
            <Agents key={project.root} root={project.root} methods={methods} providers={providers} />
          )}
          {view === "context" && canAuditContext && (
            <ContextAudit key={project.root} root={project.root} methods={methods} providers={providers} />
          )}

          {view === "work" && project.initialized && (project.storage === "CURRENT" || project.storage === "ABSENT") && methods.includes("work.tree") && (
            <WorkItems
              key={project.root}
              root={project.root}
              methods={methods}
              providers={providers}
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

  function disconnected(error = "The sidecar connection closed.") {
    request.current += 1;
    setSnapshot({ connection: "disconnected", error });
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
          <p>Projects and Work Items</p>
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

      {ready && <Projects methods={snapshot.handshake.methods} providers={snapshot.providers.providers} />}

      {ready && <Providers providers={snapshot.providers.providers} />}
    </main>
  );
}
