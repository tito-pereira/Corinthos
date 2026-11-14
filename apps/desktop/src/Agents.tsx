// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState, type FormEvent } from "react";
import { bridge, type AgentSession, type ContextProjection, type ProjectTarget, type ProviderStatus, type SessionEvent, type WorkNode } from "./bridge";

import { ProviderSwitch } from "./ProviderSwitch";

const activeStates = ["STARTING", "RUNNING", "CANCELLING"];
const defaultTimeout = "120";
const eventLimit = 10;
const visible = (value: string | number | null) => value ?? "UNKNOWN";

function SessionFacts({ session }: { session: AgentSession }) {
  const fields = {
    State: session.state,
    "Work Item": session.work_item_id,
    Provider: session.provider,
    Generation: session.run_generation,
    "Read-only execution": session.read_only ? "Yes" : "No",
    "Provider version": session.provider_version,
    Model: session.model,
    Created: session.created_at,
    Started: session.started_at,
    Updated: session.updated_at,
    Finished: session.finished_at,
    "Input tokens": session.input_tokens,
    "Output tokens": session.output_tokens,
    "Cached tokens": session.cached_tokens,
    "Duration (ms)": session.duration_ms,
    "Reported cost": session.reported_cost === null
      ? null
      : [session.reported_cost, session.reported_cost_currency].filter(Boolean).join(" "),
  };
  return (
    <dl className="facts">
      {Object.entries(fields).map(([name, value]) => (
        <div key={name}><dt>{name}</dt><dd>{visible(value)}</dd></div>
      ))}
    </dl>
  );
}

export function Agents({ root, methods, providers, initialId, openTarget }: {
  root: string;
  methods: string[];
  providers: ProviderStatus[];
  initialId?: string;
  openTarget: (target: ProjectTarget) => void;
}) {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [work, setWork] = useState<WorkNode[]>([]);
  const [detail, setDetail] = useState<AgentSession | null>(null);
  const [binding, setBinding] = useState<ContextProjection | null>(null);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [cursor, setCursor] = useState(0);
  const [moreEvents, setMoreEvents] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [workId, setWorkId] = useState("");
  const [provider, setProvider] = useState("");
  const [timeout, setTimeoutSeconds] = useState(defaultTimeout);
  const [instruction, setInstruction] = useState("");
  const [recoveryInstruction, setRecoveryInstruction] = useState("");
  const [readOnly, setReadOnly] = useState(false);
  const inFlight = useRef(false);
  const passivePoll = useRef<Promise<void> | null>(null);
  const canStart = methods.includes("session.start");
  const canGet = methods.includes("session.get");
  const canReadEvents = methods.includes("session.events");
  const canReadBinding = methods.includes("context.for_session");

  async function loadSessions(target: number) {
    const page = await bridge.sessions(root, target);
    setSessions(page.sessions);
    setOffset(target);
    setNextOffset(page.next_offset);
  }

  async function loadEvents(sessionId: string, after: number) {
    if (!canReadEvents) return;
    const page = await bridge.sessionEvents(root, sessionId, after, eventLimit);
    setEvents((previous) => [...(after === 0 ? [] : previous), ...page.events].slice(-eventLimit));
    setCursor(page.next_after_sequence);
    setMoreEvents(page.events.length === eventLimit);
  }

  async function select(session: AgentSession) {
    setDetail(session);
    setBinding(null);
    setRecoveryInstruction("");
    setEvents([]);
    setCursor(0);
    setMoreEvents(false);
    await loadEvents(session.session_id, 0);
    if (canReadBinding) setBinding((await bridge.contextForSession(root, session.session_id)).projection);
  }

  async function run(action: () => Promise<void>, message: string) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      await passivePoll.current;
      await action();
    } catch {
      setError(message);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  useEffect(() => {
    let mounted = true;
    inFlight.current = true;
    async function load() {
      try {
        const page = await bridge.sessions(root, 0);
        if (!mounted) return;
        setSessions(page.sessions);
        setNextOffset(page.next_offset);
        if (canStart && methods.includes("work.tree")) {
          const result = await bridge.workTree(root, 0);
          if (mounted) setWork(result.nodes);
        }
        if (initialId && canGet) {
          const selected = await bridge.sessionGet(root, initialId);
          if (mounted) await select(selected);
        }
      } catch {
        if (mounted) setError("Could not load the Agents view. Refresh to retry.");
      } finally {
        if (mounted) {
          inFlight.current = false;
          setLoading(false);
        }
      }
    }
    void load();
    return () => { mounted = false; };
  }, [root, methods, canStart, canGet, initialId]);

  const shouldPoll = Boolean(detail && activeStates.includes(detail.state))
    || sessions.some((session) => activeStates.includes(session.state));

  useEffect(() => {
    if (!shouldPoll) return;
    let mounted = true;
    async function poll() {
      try {
        const page = await bridge.sessions(root, offset);
        if (!mounted) return;
        setSessions(page.sessions);
        setNextOffset(page.next_offset);
        if (detail && canGet) {
          const current = await bridge.sessionGet(root, detail.session_id);
          if (!mounted) return;
          setDetail(current);
          await loadEvents(current.session_id, cursor);
          if (canReadBinding && !binding) setBinding((await bridge.contextForSession(root, current.session_id)).projection);
        }
      } catch {
        if (mounted) setError("Could not refresh the active sessions. Refresh to retry.");
      }
    }
    const timer = setInterval(() => {
      if (inFlight.current || passivePoll.current) return;
      passivePoll.current = poll().finally(() => { passivePoll.current = null; });
    }, 1000);
    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, [root, offset, detail?.session_id, shouldPoll, cursor, canGet, canReadEvents, canReadBinding, binding]);

  async function refresh(target: number) {
    await run(async () => {
      await loadSessions(target);
      if (detail && canGet) {
        const value = await bridge.sessionGet(root, detail.session_id);
        setDetail(value);
        await loadEvents(value.session_id, cursor);
        if (canReadBinding && !binding) setBinding((await bridge.contextForSession(root, value.session_id)).projection);
      }
    }, "Could not refresh sessions. Try again.");
  }

  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canStart || !provider || !workId.trim()) return;
    await run(async () => {
      const next = await bridge.sessionStart(root, {
        provider,
        work_item_id: workId.trim(),
        timeout_seconds: Number(timeout),
        read_only: readOnly,
        ...(instruction.trim() ? { instruction } : {}),
      });
      setInstruction("");
      await select(next);
      await loadSessions(0);
    }, "Could not confirm the session start. Refresh sessions before trying again.");
  }

  async function actOnSession(session: AgentSession, recover: boolean) {
    await run(async () => {
      const next = recover
        ? await bridge.sessionRecover(root, session.session_id, recoveryInstruction)
        : await bridge.sessionCancel(root, session.session_id, session.run_generation);
      setDetail(next);
      await loadEvents(next.session_id, cursor);
      await loadSessions(offset);
    }, recover
      ? "Could not recover this session. Verify the original instructions or use the terminal."
      : "Could not confirm cancellation. Refresh the session before trying again.");
  }

  return (
    <div className="work-items agents">
      <div className="section-heading">
        <h3>Agent Sessions</h3>
        <button type="button" disabled={busy || loading} onClick={() => void refresh(offset)}>Refresh sessions</button>
      </div>
      {loading && <p role="status">Loading Agent Sessions…</p>}
      {error && <p role="alert">{error}</p>}
      {!loading && !error && sessions.length === 0 && <p>No Agent Sessions on this page.</p>}
      <ul className="work-tree session-list">
        {sessions.map((session) => (
          <li key={session.session_id} data-state={session.state}>
            <button type="button" aria-pressed={detail?.session_id === session.session_id} disabled={!canGet || busy}
              onClick={() => void run(async () => select(await bridge.sessionGet(root, session.session_id)), "Could not load this session.")}
            >{session.session_id}</button>
            <span>{session.provider} · {session.state} · {session.work_item_id}</span>
          </li>
        ))}
      </ul>
      <div className="field-row">
        {offset > 0 && <button type="button" disabled={busy} onClick={() => void refresh(0)}>First sessions</button>}
        {nextOffset !== null && <button type="button" disabled={busy} onClick={() => void refresh(nextOffset)}>More sessions</button>}
      </div>
      {detail && (
        <div className="work-selection session-detail">
          <h4>{detail.session_id}</h4>
          <SessionFacts session={detail} />
          {binding && (
            <dl className="facts session-projection">
              <div><dt>Context projection</dt><dd><code>{binding.projection_id}</code></dd></div>
              <div><dt>Context digest</dt><dd><code>{binding.projection_digest}</code></dd></div>
            </dl>
          )}
          <div className="field-row">
            {methods.includes("work.get") && <button type="button" className="secondary" disabled={busy}
              onClick={() => openTarget({ tab: "work", id: detail.work_item_id })}>Open Work Item</button>}
            {binding && methods.includes("context.get") && <button type="button" className="secondary" disabled={busy}
              onClick={() => openTarget({ tab: "context", id: binding.projection_id })}>Open Context Projection</button>}
            {detail.can_cancel && methods.includes("session.cancel") && (
              <button type="button" disabled={busy} onClick={() => void actOnSession(detail, false)}>Cancel session</button>
            )}
            {detail.can_recover && methods.includes("session.recover") && (
              <form onSubmit={(event) => { event.preventDefault(); void actOnSession(detail, true); }}>
                <label htmlFor="session-recovery-instruction">Original instructions (if supplied)</label>
                <input id="session-recovery-instruction" maxLength={4096} disabled={busy}
                  value={recoveryInstruction} onChange={(event) => setRecoveryInstruction(event.target.value)} />
                <button type="submit" disabled={busy}>Recover session</button>
              </form>
            )}
          </div>
          {detail.cleanup_unconfirmed && <p>Process cleanup could not be confirmed.</p>}
          {detail.state === "INTERRUPTED" && !detail.can_recover && (
            <p>Native recovery is unavailable for this session.</p>
          )}
          {methods.includes("switch.status") && methods.includes("switch.execute") && (
            <ProviderSwitch key={detail.session_id + ":" + detail.run_generation}
              root={root} source={detail} providers={providers} disabled={busy}
              openTarget={openTarget} canOpenProjection={methods.includes("context.get")} canOpenHandoff={methods.includes("handoff.get")}
              onSession={(sessionId) => void run(async () => {
                await select(await bridge.sessionGet(root, sessionId));
                await loadSessions(0);
              }, "Could not open the target session.")} />
          )}
          {canReadEvents && (
            <>
              <h4>Recent events</h4>
              {events.length === 0 && <p>No events recorded.</p>}
              <ol className="session-events">
                {events.map((event) => (
                  <li key={event.sequence}>
                    <strong>{event.kind}</strong> <time>{event.occurred_at}</time>
                    {Object.keys(event.payload).length > 0 && <code>{JSON.stringify(event.payload)}</code>}
                  </li>
                ))}
              </ol>
              {moreEvents && <button type="button" disabled={busy} onClick={() => void run(
                () => loadEvents(detail.session_id, cursor),
                "Could not load more session events.",
              )}>More events</button>}
            </>
          )}
        </div>
      )}
      {canStart && (
        <form className="work-form" onSubmit={(event) => void start(event)}>
          <h4>Start an Agent Session</h4>
          <fieldset disabled={loading || busy}>
            <label htmlFor="session-work">Work Item ID</label>
            <input id="session-work" list="session-work-items" required maxLength={128}
              value={workId} onChange={(event) => setWorkId(event.target.value)} />
            <datalist id="session-work-items">
              {work.map((item) => <option key={item.work_item_id} value={item.work_item_id}>{item.objective}</option>)}
            </datalist>
            <label htmlFor="session-provider">Provider</label>
            <select id="session-provider" required value={provider} onChange={(event) => setProvider(event.target.value)}>
              <option value="">Select provider</option>
              {providers.filter((choice) => choice.supported).map((choice) => <option key={choice.provider}>{choice.provider}</option>)}
            </select>
            <label htmlFor="session-timeout">Timeout (seconds)</label>
            <input id="session-timeout" type="number" min={1} max={3600} step={1} required
              value={timeout} onChange={(event) => setTimeoutSeconds(event.target.value)} />
            <label htmlFor="session-instruction">Instructions (optional)</label>
            <textarea id="session-instruction" value={instruction} maxLength={4096}
              onChange={(event) => setInstruction(event.target.value)} />
            <label className="checkbox">
              <input type="checkbox" checked={readOnly} onChange={(event) => setReadOnly(event.target.checked)} /> Read-only execution
            </label>
            <button type="submit" disabled={!provider || !workId.trim()}>Start session</button>
          </fieldset>
        </form>
      )}
    </div>
  );
}
