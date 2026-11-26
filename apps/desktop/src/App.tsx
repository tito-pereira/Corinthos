// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState, type ReactNode } from "react";
import { bridge, type BackendSnapshot, type Conversation, type GuiOperation, type ProjectRef, type ProviderStatus, type RunStatus } from "./bridge";
import { ConversationView } from "./ConversationView";
import { ProductUpdate } from "./ProductUpdate";
import reference from "./assets/branding/corinthos-reference.png";

const active = (status?: string) => status === "STARTING" || status === "RUNNING";
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);
type ModalState = null | "create" | "provider" | "update" | "ideas" | "tasks" | "discard";
type Recent = ProjectRef & { available: boolean };
type RunTarget = { project: ProjectRef; run_id: string };

function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const panel = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const node = panel.current;
    (node?.querySelector<HTMLElement>("input, textarea") || node?.querySelector<HTMLElement>("button"))?.focus();
    function keys(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); close.current(); }
      if (event.key === "Tab" && node) {
        const controls = Array.from(node.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex='0']"));
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    }
    document.addEventListener("keydown", keys);
    return () => { document.removeEventListener("keydown", keys); previous?.focus(); };
  }, []);
  return <div className="overlay"><section ref={panel} className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <header><h2 id="modal-title">{title}</h2><button type="button" className="close" aria-label="Close dialog" onClick={onClose}>×</button></header>
    {children}
  </section></div>;
}

export function App() {
  const [backend, setBackend] = useState<BackendSnapshot | null>(null);
  const [project, setProject] = useState<ProjectRef | null>(null);
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [provider, setProvider] = useState("");
  const [recent, setRecent] = useState<Recent[]>([]);
  const [base, setBase] = useState("");
  const [showRecent, setShowRecent] = useState(false);
  const [modal, setModal] = useState<ModalState>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [picking, setPicking] = useState(false);
  const [conversations, setConversations] = useState<Record<string, Conversation>>({});
  const [currentConversation, setCurrentConversation] = useState("");
  const [operations, setOperations] = useState<Record<string, GuiOperation>>({});
  const [runStatuses, setRunStatuses] = useState<Record<string, RunStatus & {discarded?: boolean}>>({});
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [creationId, setCreationId] = useState("");
  const [providerName, setProviderName] = useState("local");
  const [command, setCommand] = useState("");
  const [providerReturn, setProviderReturn] = useState<ModalState>(null);
  const [summary, setSummary] = useState<{empty: boolean; items: string[]} | null>(null);
  const [summaryProject, setSummaryProject] = useState("");
  const [discardTarget, setDiscardTarget] = useState<RunTarget | null>(null);
  const [discardSummary, setDiscardSummary] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const state = useRef({ conversations, operations, runStatuses, project, creationId, modal });
  state.current = { conversations, operations, runStatuses, project, creationId, modal };
  const scope = useRef("main");
  const modalGeneration = useRef(0);
  const polling = useRef(false);
  const mounted = useRef(true);
  const streams = useRef<Record<string, { cursor: number; text: string; complete: boolean }>>({});
  const retiredRuns = useRef(new Set<string>());
  const ready = backend?.connection === "ready";

  function rememberOperation(value: GuiOperation) {
    setOperations(previous => ({ ...previous, [value.operation_id]: value }));
  }
  function mergeConversation(value: Conversation) {
    value = { ...value, messages: value.messages.map(message => {
      const stream = message.run_id ? streams.current[value.conversation_id + ":" + message.run_id] : undefined;
      return stream?.text ? { ...message, text: stream.text, truncated: !stream.complete && message.truncated } : message;
    }) };
    setConversations(previous => ({ ...previous, [value.conversation_id]: value }));
    return value;
  }
  async function readRecents() {
    const value = await bridge.projects();
    if (mounted.current) { setRecent(value.projects); setBase(value.base); }
  }
  async function selectProject(selected: ProjectRef | null) {
    scope.current = selected?.project_id || "main";
    const requested = scope.current;
    setProject(selected); setShowRecent(false); setError(""); setCurrentConversation("");
    const value = await bridge.conversation(selected);
    if (!mounted.current) return;
    mergeConversation(value);
    if (scope.current === requested) setCurrentConversation(value.conversation_id);
    for (const message of value.messages) {
      if (message.operation_id && (message.run_id || active(message.status))) {
        try { rememberOperation(await bridge.operation(message.operation_id)); }
        catch { /* Retained runs are restored below even after bounded operation history expires. */ }
      }
    }
    if (selected) {
      const retained = await bridge.runs(selected);
      for (const run of retained.runs) {
        const identity = "saved-" + run.run_id;
        rememberOperation({operation_id: identity, kind: run.skill === "conversation" ? "conversation" : "execute", status: run.status, phase: "Retained run", project: selected, run: {run_id: run.run_id, project_id: selected.project_id}});
        setRunStatuses(previous => ({...previous, [selected.project_id + ":" + run.run_id]: run}));
      }
    }
  }
  async function connect(restart = false) {
    setError("");
    try {
      const value = await (restart ? bridge.restart() : bridge.snapshot());
      if (!mounted.current) return;
      setBackend(value);
      if (value.connection === "ready") {
        setProviders(value.providers.providers);
        setProvider(previous => previous || value.providers.providers.find(item => item.available)?.provider || "");
        try { await readRecents(); } catch (failure) { setError(errorText(failure)); }
        await selectProject(state.current.project);
      } else setError(value.error || "The local backend is unavailable.");
    } catch (failure) { if (mounted.current) setError(errorText(failure)); }
  }
  useEffect(() => {
    mounted.current = true;
    void connect();
    const subscription = bridge.onDisconnect(() => { setBackend(null); setError("The local backend disconnected. Reconnect to inspect running work."); });
    return () => { mounted.current = false; void subscription.then(stop => stop()); };
  }, []);

  useEffect(() => {
    if (!ready) return;
    const timer = setInterval(() => void poll(), 450);
    return () => clearInterval(timer);
  }, [ready]);

  async function poll() {
    if (polling.current) return;
    polling.current = true;
    try {
      for (const previous of Object.values(state.current.operations).filter(item => active(item.status) && !item.operation_id.startsWith("saved-"))) {
        const value = await bridge.operation(previous.operation_id);
        if (!mounted.current) return;
        rememberOperation(value);
        if (value.kind === "conversation") {
          const conversation = Object.values(state.current.conversations).find(item => item.messages.some(message => message.operation_id === value.operation_id));
          if (conversation) mergeConversation(await bridge.conversationGet(conversation.conversation_id));
        }
        if (value.operation_id === state.current.creationId && !active(value.status)) {
          setCreationId("");
          if (value.status === "SUCCEEDED" && value.project) {
            setModal(null); setName(""); setDescription(""); setNotice(value.message || "Project ready.");
            await readRecents(); await selectProject(value.project);
          } else if (value.status === "CANCELLED") { setModal(null); setNotice(value.error || "Cancelled."); }
          else setError(value.error || "Project creation failed.");
        } else if (value.kind === "import" && !active(value.status)) {
          if (value.status === "SUCCEEDED" && value.project) { setNotice(value.message || "Project imported."); await readRecents(); await selectProject(value.project); }
          else setError(value.error || "Import stopped.");
        }
        if (value.kind === "action" && !active(value.status)) {
          if (value.status === "FAILED") setError(value.error || "The original run could not be changed.");
          else if (value.result?.discarded && value.project) {
            const key = value.project.project_id + ":" + value.result.discarded;
            retiredRuns.current.add(key);
            setRunStatuses(previousStatuses => ({...previousStatuses, [key]: {...previousStatuses[key], discarded: true}}));
            for (const key of Object.keys(streams.current)) if (key.endsWith(":" + value.result.discarded)) streams.current[key].complete = true;
            const origin = Object.values(state.current.conversations).find(item => item.project?.project_id === value.project!.project_id);
            if (origin) mergeConversation(await bridge.conversationGet(origin.conversation_id));
            setNotice("Run discarded after retaining its summary.");
          } else if (value.result?.applied_count !== undefined) {
            setNotice(`Applied ${value.result.applied_count} changed paths to ${value.project?.name}.`);
            if (value.project && value.run) {
              const key = value.project.project_id + ":" + value.run.run_id;
              setRunStatuses(previous => ({...previous, [key]: {...previous[key], integrated: true}}));
            }
          }
        }
      }
      for (const conversation of Object.values(state.current.conversations)) {
        for (const message of conversation.messages) {
          if (!message.run_id) continue;
          if (conversation.project && retiredRuns.current.has(conversation.project.project_id + ":" + message.run_id)) continue;
          const key = conversation.conversation_id + ":" + message.run_id;
          const stream = streams.current[key] || {cursor: 0, text: "", complete: false};
          if (!stream.complete) {
            const chunk = await bridge.output(conversation.conversation_id, message.run_id, stream.cursor);
            const updated = {cursor: chunk.cursor, text: (stream.text + chunk.text).slice(0, 1024 * 1024), complete: chunk.complete};
            streams.current[key] = updated;
            setConversations(previous => {
              const latest = previous[conversation.conversation_id];
              if (!latest || !updated.text) return previous;
              return {...previous, [conversation.conversation_id]: {...latest, messages: latest.messages.map(current =>
                current.role === "assistant" && current.run_id === message.run_id
                  ? {...current, text: updated.text, truncated: !updated.complete && current.truncated}
                  : current)}};
            });
          }
        }
      }
      const cards = Object.values(state.current.operations).filter(item => item.project && item.run && item.kind !== "action" && item.kind !== "create");
      for (const card of cards) {
        const key = card.project!.project_id + ":" + card.run!.run_id;
        if (retiredRuns.current.has(key) || state.current.runStatuses[key]?.discarded) continue;
        if (state.current.runStatuses[key] && !active(state.current.runStatuses[key].status) && !Object.values(state.current.operations).some(item => item.kind === "action" && item.run?.run_id === card.run!.run_id && active(item.status))) continue;
        const value = await bridge.run(card.project!, card.run!.run_id);
        setRunStatuses(previous => ({...previous, [key]: value}));
      }
    } catch (failure) { if (mounted.current) setError(errorText(failure)); }
    finally { polling.current = false; }
  }

  async function pick(mode: "open" | "import") {
    if (picking) return;
    setPicking(true); setError(""); setNotice(""); setShowRecent(false);
    try {
      const path = await bridge.selectProjectDirectory();
      if (path === null) return;
      if (mode === "open") { const selected = await bridge.openProject(path); await readRecents(); await selectProject(selected); }
      else {
        const result = await bridge.importProject(path);
        if (result.project) { await readRecents(); await selectProject(result.project); }
        else if (result.operation) { rememberOperation(result.operation); setNotice("Importing an independent working copy; the original source will remain unchanged."); }
      }
    } catch (failure) { setError(errorText(failure)); }
    finally { setPicking(false); }
  }
  async function resume(entry: Recent) {
    try { const selected = await bridge.openProject(entry.root); await readRecents(); await selectProject(selected); }
    catch (failure) { setError(errorText(failure)); }
  }
  async function send(message: string) {
    const origin = state.current.conversations[currentConversation];
    if (!origin || !provider) { setError("Configure and select an available provider first."); return; }
    setError("");
    try { const op = await bridge.send(origin.conversation_id, message, provider); rememberOperation(op); mergeConversation(await bridge.conversationGet(origin.conversation_id)); }
    catch (failure) { setError(errorText(failure)); }
  }
  async function createProject() {
    setSubmitting(true); setError("");
    try { const value = await bridge.createProject(name, description, provider); setCreationId(value.operation_id); rememberOperation(value); }
    catch (failure) { setError(errorText(failure)); }
    finally { setSubmitting(false); }
  }
  async function closeModal() {
    if (creationId && active(operations[creationId]?.status)) {
      try {
        const result = await bridge.cancelOperation(creationId);
        rememberOperation(result);
        if (!active(result.status)) {
          setCreationId("");
          if (result.status === "SUCCEEDED" && result.project) {setModal(null); await readRecents(); await selectProject(result.project);}
          else if (result.status === "CANCELLED") {setModal(null); setNotice(result.error || "Cancelled.");}
          else setError(result.error || "Creation stopped.");
        }
      } catch (failure) { setError(errorText(failure)); }
      return;
    }
    modalGeneration.current++; setModal(null); setError("");
  }
  async function openSummary(kind: "ideas" | "tasks") {
    if (!project) return;
    const generation = ++modalGeneration.current;
    const origin = project;
    setModal(kind); setSummary(null); setSummaryProject(origin.name); setError("");
    try { const result = await bridge.summary(origin, kind); if (generation === modalGeneration.current) setSummary(result); }
    catch (failure) { if (generation === modalGeneration.current) setError(errorText(failure)); }
  }
  async function executeTasks() {
    if (!project) return;
    setError("");
    try { const result = await bridge.execute(project, provider); if (result.empty) setNotice("No Tasks Yet"); else if (result.operation) rememberOperation(result.operation); }
    catch (failure) { setError(errorText(failure)); }
  }
  async function actOnRun(target: RunTarget, action: "apply" | "discard" | "cancel", summary = "") {
    setError("");
    try { rememberOperation(await bridge.runAction(target.project, target.run_id, action, summary)); if (action === "discard") setModal(null); }
    catch (failure) { setError(errorText(failure)); }
  }
  async function saveProvider() {
    setSubmitting(true); setError("");
    try {
      const parsed: unknown = JSON.parse(command);
      if (!Array.isArray(parsed) || !parsed.every(item => typeof item === "string")) throw new Error("Enter a JSON array containing the native executable and arguments.");
      const result = await bridge.providerConfigure(providerName, parsed);
      setProviders(result.providers); setProvider(providerName); setModal(providerReturn); setProviderReturn(null);
    } catch (failure) { setError(errorText(failure)); }
    finally { setSubmitting(false); }
  }
  const conversation = conversations[currentConversation];
  const busy = Boolean(conversation?.messages.some(message => active(message.status)));
  const currentOperation = conversation?.messages.filter(message => active(message.status)).at(-1)?.operation_id;
  const cards = Object.values(operations).filter(item => project && item.project?.project_id === project.project_id && item.run && ["conversation", "execute"].includes(item.kind)).filter((item,index,all) => all.findIndex(other => other.run?.run_id === item.run?.run_id) === index);
  const toolbar = <div className="provider-toolbar"><label>Provider<select aria-label="Provider" value={provider} onChange={event => setProvider(event.target.value)} disabled={!ready}>
    <option value="">Select provider</option>{providers.map(item => <option key={item.provider} value={item.provider} disabled={!item.available}>{item.provider}{item.available ? "" : " (unavailable)"}</option>)}
  </select></label><button type="button" onClick={() => {setProviderReturn(null); setError(""); setModal("provider");}} disabled={!ready}>Configure provider</button></div>;
  const actions = <div className="run-cards">{cards.map(card => {
    const target = {project: card.project!, run_id: card.run!.run_id};
    const key = target.project.project_id + ":" + target.run_id;
    const run = runStatuses[key];
    if (run?.discarded) return null;
    const actionPending = Object.values(operations).some(item => item.kind === "action" && item.run?.run_id === target.run_id && active(item.status));
    return <section className="run-card" key={card.operation_id} aria-label={`Run for ${target.project.name}`}><strong>{card.kind === "execute" ? "Task run" : "Conversation run"} · {target.project.name}</strong>
      <p role="status">{run?.integrated ? "Changes applied" : (run?.status || card.status) + " · changes remain isolated"}</p>
      {card.error && <p role="alert">{card.error}</p>}
      <div className="button-row">{active(run?.status || card.status) ? <button disabled={actionPending} onClick={() => void actOnRun(target, "cancel")}>Cancel run</button> : <>
        {run?.status === "SUCCEEDED" && !run.integrated && <button disabled={actionPending} onClick={() => void actOnRun(target, "apply")}>Apply Changes</button>}
        <button disabled={actionPending} onClick={() => {setDiscardTarget(target); setDiscardSummary("Reviewed this run and retained its outcome."); setModal("discard");}}>Discard</button>
      </>}</div></section>;
  })}</div>;

  return <><div className="app-shell" inert={modal !== null} data-gui-ready={ready ? "true" : "false"} data-page={project ? "project" : "main"}>
    <aside className="sidebar"><button className="brand" aria-label="Main page" onClick={() => void selectProject(null).catch(failure => setError(errorText(failure)))}><img src={reference} alt="Corinthos — four-column temple and silver wordmark" /></button>
      <nav aria-label={project ? "Project navigation" : "Main navigation"}>{project ? <>
        <p className="project-name" title={project.root}>{project.name}</p>
        <button onClick={() => void openSummary("ideas")}>Future Ideas</button><button onClick={() => void openSummary("tasks")}>Task List</button><button onClick={() => void executeTasks()} disabled={!ready}>Execute Tasks</button>
      </> : <>
        <button onClick={() => {setError(""); setModal("create");}} disabled={!ready}>Create Project</button>
        <button aria-expanded={showRecent} onClick={() => {setShowRecent(!showRecent); if (!showRecent) void readRecents().catch(failure => setError(errorText(failure)));}} disabled={!ready || picking}>Resume Project</button>
        {showRecent && <div className="recents" role="group" aria-label="Recent projects">{recent.slice(0,10).map((entry, index) => <button key={entry.root + index} disabled={!entry.available} onClick={() => void resume(entry)} title={entry.root}><strong>{entry.name}</strong><small>{entry.available ? entry.root : "Unavailable — choose another folder"}</small></button>)}<button className="choose-other" onClick={() => void pick("open")}>... <span>Choose Other</span></button></div>}
        <button onClick={() => void pick("import")} disabled={!ready || picking}>Import Project</button>
      </>}</nav>
      <footer><button className="update-button" onClick={() => {setError(""); setModal("update");}}>Update</button><small>{backend?.desktop.version || "Corinthos"} · {ready ? "Connected" : "Connecting"}</small></footer>
    </aside>
    <main>{!ready && <div className="connection-state" role="status">{error || "Connecting to the local backend…"}<button onClick={() => void connect(true)}>Reconnect</button></div>}
      {picking && <p className="notice" role="status">Choose a local project folder. Cancel leaves the project unchanged.</p>}
      {notice && <p className="notice" role="status">{notice}<button aria-label="Dismiss notice" onClick={() => setNotice("")}>×</button></p>}
      {error && ready && <p className="error-banner" role="alert">{error}<button aria-label="Dismiss error" onClick={() => setError("")}>×</button></p>}
      {project?.import_source && <p className="copy-note">Independent working copy · original source preserved at <span title={project.import_source}>{project.import_source}</span></p>}
      <ConversationView key={currentConversation || scope.current} title={project ? `This is the ${project.name} project page` : "Welcome to Corinthos"} messages={conversation?.messages || []} busy={busy || !ready || !conversation} error={conversation?.omitted ? "Older messages were omitted from this bounded view." : undefined} onSend={message => void send(message)} onCancel={currentOperation ? () => void bridge.cancelOperation(currentOperation).then(rememberOperation).catch(failure => setError(errorText(failure))) : undefined} toolbar={toolbar} actions={actions} />
    </main>
  </div>
  {modal && <Modal title={modal === "create" ? "Create Project" : modal === "provider" ? "Configure provider" : modal === "update" ? "Update Corinthos" : modal === "discard" ? "Discard isolated run" : `${modal === "ideas" ? "Future Ideas" : "Task List"} — ${summaryProject}`} onClose={() => void closeModal()}>
    {error && <p role="alert">{error}</p>}
    {modal === "create" && <form onSubmit={event => {event.preventDefault(); void createProject();}}>
      <label htmlFor="project-name">Project name</label><input id="project-name" value={name} maxLength={80} required disabled={Boolean(creationId)} onChange={event => setName(event.target.value)} />
      <label htmlFor="project-description">Project description</label><textarea id="project-description" value={description} maxLength={4000} disabled={Boolean(creationId)} onChange={event => setDescription(event.target.value)} />
      <p className="destination">Destination: <span>{base}{base.includes("\\") ? "\\" : "/"}{name || "<project name>"}</span></p>
      {!provider && <p>Select a provider in the composer before creating a project. <button type="button" onClick={() => {setProviderReturn("create"); setModal("provider");}}>Configure provider</button></p>}
      {creationId && <p role="status">{operations[creationId]?.phase || "Preparing"}{operations[creationId]?.cancel_requested ? " · cancellation pending" : ""}</p>}
      <div className="button-row"><button type="button" onClick={() => void closeModal()}>Cancel</button><button className="primary" disabled={submitting || Boolean(creationId) || !provider} type="submit">Create Project</button></div>
    </form>}
    {modal === "provider" && <form onSubmit={event => {event.preventDefault(); void saveProvider();}}><p>Use an installed native executable that accepts the prompt on standard input. Provider sign-in uses its own normal configuration.</p><label htmlFor="provider-name">Provider name</label><input id="provider-name" value={providerName} maxLength={64} required onChange={event => setProviderName(event.target.value)} /><label htmlFor="provider-command">Executable and arguments (JSON array)</label><textarea id="provider-command" value={command} required placeholder={'["C:\\Tools\\provider.exe", "argument"]'} onChange={event => setCommand(event.target.value)} /><div className="button-row"><button type="button" onClick={() => void closeModal()}>Cancel</button><button className="primary" disabled={submitting}>Save provider</button></div></form>}
    {modal === "update" && <ProductUpdate methods={backend?.connection === "ready" ? backend.handshake.methods : []} />}
    {(modal === "ideas" || modal === "tasks") && <>{!summary && !error ? <p role="status">Reading current project content…</p> : summary?.empty ? <p className="empty">Nothing Yet</p> : <ul className="summary-list">{summary?.items.map((item,index) => <li key={index}>{item}</li>)}</ul>}</>}
    {modal === "discard" && discardTarget && <form onSubmit={event => {event.preventDefault(); void actOnRun(discardTarget, "discard", discardSummary);}}><p>Discard the isolated output for <strong>{discardTarget.project.name}</strong>. Existing project files are preserved.</p><label htmlFor="discard-summary">Retained outcome summary</label><textarea id="discard-summary" value={discardSummary} required maxLength={2048} onChange={event => setDiscardSummary(event.target.value)} /><div className="button-row"><button type="button" onClick={() => void closeModal()}>Cancel</button><button className="primary">Discard</button></div></form>}
  </Modal>}
  </>;
}
