// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  bridge, type AgentSession, type ContextProjection, type ContextSource,
  type ProjectTarget, type ProviderStatus, type SessionHandoff, type WorkNode,
} from "./bridge";

function ProjectionFacts({ projection }: { projection: ContextProjection }) {
  return (
    <dl className="facts">
      <div><dt>Projection</dt><dd><code>{projection.projection_id}</code></dd></div>
      <div><dt>Work Item</dt><dd><code>{projection.work_item_id}</code></dd></div>
      <div><dt>Target provider</dt><dd>{projection.target_provider}</dd></div>
      <div><dt>Selector policy</dt><dd>{projection.selector_policy_version}</dd></div>
      <div><dt>Redaction policy</dt><dd>{projection.redaction_policy_version}</dd></div>
      <div><dt>Created</dt><dd><time>{projection.created_at}</time></dd></div>
      <div><dt>Projection digest</dt><dd><code>{projection.projection_digest}</code></dd></div>
      <div><dt>Source set digest</dt><dd><code>{projection.source_set_digest}</code></dd></div>
    </dl>
  );
}

function SourceFacts({ source }: { source: ContextSource }) {
  return (
    <li>
      <strong>{source.kind} · {source.authority}</strong>
      <dl className="facts">
        <div><dt>Identity</dt><dd><code>{source.identity}</code></dd></div>
        {source.revision !== null && <div><dt>Revision or cutoff</dt><dd>{source.revision}</dd></div>}
        <div><dt>Digest</dt><dd><code>{source.digest}</code></dd></div>
        <div><dt>Selection reason</dt><dd>{source.reason}</dd></div>
        <div><dt>Redaction</dt><dd>{source.redaction.length ? source.redaction.join(", ") : "None applied"}</dd></div>
      </dl>
    </li>
  );
}

function HandoffFacts({ handoff, openSession }: { handoff: SessionHandoff; openSession?: () => void }) {
  return (
    <div className="work-selection handoff-detail">
      <h4>{handoff.handoff_id}</h4>
      <dl className="facts">
        <div><dt>Authority</dt><dd>DERIVED</dd></div>
        <div><dt>Source session</dt><dd><code>{handoff.source_session_id}</code></dd></div>
        <div><dt>Work Item</dt><dd><code>{handoff.work_item_id}</code></dd></div>
        <div><dt>Generation</dt><dd>{handoff.run_generation}</dd></div>
        <div><dt>Event cutoff</dt><dd>{handoff.event_cutoff}</dd></div>
        <div><dt>Provider</dt><dd>{handoff.provider}</dd></div>
        <div><dt>Terminal state</dt><dd>{handoff.terminal_state}</dd></div>
        <div><dt>Handoff digest</dt><dd><code>{handoff.digest}</code></dd></div>
        <div><dt>Created</dt><dd><time>{handoff.created_at}</time></dd></div>
      </dl>
      {openSession && <button type="button" className="secondary" onClick={openSession}>Open Source Session</button>}
    </div>
  );
}

export function ContextAudit({ root, methods, providers, initialId, initialHandoff, openTarget }: {
  root: string;
  methods: string[];
  providers: ProviderStatus[];
  initialId?: string;
  initialHandoff?: boolean;
  openTarget: (target: ProjectTarget) => void;
}) {
  const [work, setWork] = useState<WorkNode[]>([]);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [workId, setWorkId] = useState("");
  const [provider, setProvider] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [projectionPage, setProjectionPage] = useState<Awaited<ReturnType<typeof bridge.projections>> | null>(null);
  const [handoffPage, setHandoffPage] = useState<Awaited<ReturnType<typeof bridge.handoffs>> | null>(null);
  const [sourcePage, setSourcePage] = useState<Awaited<ReturnType<typeof bridge.contextSources>> | null>(null);
  const [appliedFilter, setAppliedFilter] = useState("");
  const [projection, setProjection] = useState<ContextProjection | null>(null);
  const [handoff, setHandoff] = useState<SessionHandoff | null>(null);
  const [verification, setVerification] = useState("Not checked");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const writing = useRef(false);
  const canCreate = methods.includes("context.create");
  const canReadSources = methods.includes("context.sources");
  const canListHandoffs = methods.includes("handoff.list");
  const canCreateHandoff = methods.includes("handoff.create");

  useEffect(() => {
    let mounted = true;
    async function load() {
      try {
        const page = await bridge.projections(root, 0);
        if (!mounted) return;
        setProjectionPage(page);
        if (canListHandoffs) {
          const handoffs = await bridge.handoffs(root, 0);
          if (!mounted) return;
          setHandoffPage(handoffs);
        }
        if (methods.includes("work.tree")) {
          const list = await bridge.workTree(root, 0);
          if (mounted) setWork(list.nodes);
        }
        if (canCreateHandoff && methods.includes("session.list")) {
          const list = await bridge.sessions(root, 0);
          if (mounted) setSessions(list.sessions);
        }
        if (initialId && initialHandoff && methods.includes("handoff.get")) {
          const selected = await bridge.handoffGet(root, initialId);
          if (mounted) setHandoff(selected);
        } else if (initialId && !initialHandoff && methods.includes("context.get")) {
          const selected = await bridge.contextGet(root, initialId);
          if (mounted) await inspect(selected);
        }
      } catch {
        if (mounted) setError("Could not load the context audit.");
      } finally {
        if (mounted) setLoading(false);
      }
    }
    void load();
    return () => { mounted = false; };
  }, [root, methods, canListHandoffs, canCreateHandoff, initialId, initialHandoff]);

  async function run(action: () => Promise<void>, message: string) {
    if (writing.current) return;
    writing.current = true;
    setBusy(true);
    setError("");
    try {
      await action();
    } catch {
      setError(message);
    } finally {
      writing.current = false;
      setBusy(false);
    }
  }

  async function filter(workItem: string) {
    const page = await bridge.projections(root, 0, workItem);
    const handoffs = canListHandoffs ? await bridge.handoffs(root, 0, workItem) : null;
    setAppliedFilter(workItem);
    setProjectionPage(page);
    setHandoffPage(handoffs);
  }

  async function inspect(value: ContextProjection) {
    setProjection(value);
    setSourcePage(null);
    setVerification("Not checked");
    if (canReadSources) setSourcePage(await bridge.contextSources(root, value.projection_id, 0));
  }

  function createProjection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const item = workId.trim();
    if (!canCreate || !provider || !item) return;
    void run(async () => {
      const created = await bridge.contextCreate(root, item, provider);
      await filter(item);
      await inspect(created);
    }, "Could not confirm the projection. Refresh before trying again.");
  }

  function createHandoff(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canCreateHandoff || !sessionId.trim()) return;
    void run(async () => {
      const created = await bridge.handoffCreate(root, sessionId.trim());
      setHandoff(created);
      if (canListHandoffs) setHandoffPage(await bridge.handoffs(root, 0, appliedFilter));
    }, "Could not create a handoff. Select a session with stable history.");
  }

  const disabled = busy || loading;
  return (
    <div className="work-items context-audit">
      <div className="section-heading">
        <h3>Context audit</h3>
        <button type="button" disabled={disabled} onClick={() => void run(
          () => filter(appliedFilter), "Could not refresh the context audit.",
        )}>Refresh context</button>
      </div>
      {loading && <p role="status">Loading context audit…</p>}
      {error && <p role="alert">{error}</p>}
      <form className="work-form" onSubmit={createProjection}>
        <fieldset disabled={disabled}>
          <label htmlFor="context-work">Work Item ID</label>
          <input id="context-work" list="context-work-items" required maxLength={128}
            value={workId} onChange={(event) => setWorkId(event.target.value)} />
          <datalist id="context-work-items">
            {work.map((item) => <option key={item.work_item_id} value={item.work_item_id}>{item.scope_level} · {item.status}</option>)}
          </datalist>
          <div className="field-row">
            <button type="button" disabled={!workId.trim()} onClick={() => void run(async () => {
              await filter(workId.trim());
              setProjection(null);
              setHandoff(null);
            }, "Could not filter by Work Item.")}>Filter by Work Item</button>
            {appliedFilter && <button type="button" onClick={() => void run(async () => {
              await filter("");
              setProjection(null);
              setHandoff(null);
            }, "Could not load all project context.")}>Show all Work Items</button>}
          </div>
          {canCreate && <>
            <label htmlFor="context-provider">Target provider</label>
            <select id="context-provider" required value={provider} onChange={(event) => setProvider(event.target.value)}>
              <option value="">Select provider</option>
              {providers.filter((choice) => choice.supported).map((choice) => <option key={choice.provider}>{choice.provider}</option>)}
            </select>
            <button type="submit" disabled={!workId.trim() || !provider}>Create projection</button>
          </>}
        </fieldset>
      </form>

      <h4>Projections</h4>
      {projectionPage && projectionPage.projections.length === 0 && <p>No projections on this page.</p>}
      <ul className="work-tree">
        {projectionPage?.projections.map((value) => (
          <li key={value.projection_id}>
            <button type="button" aria-pressed={projection?.projection_id === value.projection_id} disabled={disabled}
              onClick={() => void run(async () => inspect(methods.includes("context.get")
                ? await bridge.contextGet(root, value.projection_id) : value), "Could not inspect this projection.")}>
              {value.projection_id}
            </button>
            <span>{value.target_provider} · {value.work_item_id}</span>
          </li>
        ))}
      </ul>
      {projectionPage?.next_offset != null && <button type="button" disabled={disabled} onClick={() => void run(async () => {
        setProjectionPage(await bridge.projections(root, projectionPage.next_offset!, appliedFilter));
      }, "Could not load more projections.")}>More projections</button>}
      {projection && (
        <div className="work-selection projection-detail">
          <ProjectionFacts projection={projection} />
          <div className="field-row">
            {methods.includes("work.get") && <button type="button" className="secondary" disabled={disabled}
              onClick={() => openTarget({ tab: "work", id: projection.work_item_id })}>Open Work Item</button>}
            {methods.includes("context.verify") && <button type="button" disabled={disabled} onClick={() => void run(async () => {
              setVerification("Checking…");
              try {
                const result = await bridge.contextVerify(root, projection.projection_id);
                setVerification(result.verified && result.projection_digest === projection.projection_digest
                  ? "Verified" : "Verification failed");
              } catch {
                setVerification("Verification failed");
              }
            }, "Could not verify this projection.")}>Verify projection</button>}
            <p role="status">{verification}</p>
          </div>
          <h4>Selected sources</h4>
          <ol className="context-sources" start={sourcePage?.sources[0] ? sourcePage.sources[0].ordinal + 1 : 1}>
            {sourcePage?.sources.map((source) => <SourceFacts key={source.ordinal} source={source} />)}
          </ol>
          {sourcePage?.next_offset != null && <button type="button" disabled={disabled} onClick={() => void run(async () => {
            setSourcePage(await bridge.contextSources(root, projection.projection_id, sourcePage.next_offset!));
          }, "Could not load more sources.")}>More sources</button>}
        </div>
      )}

      {canListHandoffs && <>
        <h4>Session handoffs</h4>
        {handoffPage && handoffPage.handoffs.length === 0 && <p>No handoffs on this page.</p>}
        <ul className="work-tree">
          {handoffPage?.handoffs.map((value) => (
            <li key={value.handoff_id}>
              <button type="button" aria-pressed={handoff?.handoff_id === value.handoff_id} disabled={disabled}
                onClick={() => void run(async () => setHandoff(methods.includes("handoff.get")
                  ? await bridge.handoffGet(root, value.handoff_id) : value), "Could not inspect this handoff.")}>
                {value.handoff_id}
              </button>
              <span>{value.terminal_state} · {value.work_item_id}</span>
            </li>
          ))}
        </ul>
        {handoffPage?.next_offset != null && <button type="button" disabled={disabled} onClick={() => void run(async () => {
          setHandoffPage(await bridge.handoffs(root, handoffPage.next_offset!, appliedFilter));
        }, "Could not load more handoffs.")}>More handoffs</button>}
      </>}
      {handoff && <HandoffFacts handoff={handoff}
        openSession={methods.includes("session.get") ? () => openTarget({ tab: "agents", id: handoff.source_session_id }) : undefined} />}
      {canCreateHandoff && (
        <form className="work-form" onSubmit={createHandoff}>
          <fieldset disabled={disabled}>
            <label htmlFor="handoff-session">Source Session ID</label>
            <input id="handoff-session" list="handoff-sessions" required maxLength={128}
              value={sessionId} onChange={(event) => setSessionId(event.target.value)} />
            <datalist id="handoff-sessions">
              {sessions.filter((session) => session.finished_at !== null)
                .map((session) => <option key={session.session_id} value={session.session_id}>{session.state}</option>)}
            </datalist>
            <button type="submit" disabled={!sessionId.trim()}>Create handoff</button>
          </fieldset>
        </form>
      )}
    </div>
  );
}
