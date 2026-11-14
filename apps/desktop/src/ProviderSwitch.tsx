// SPDX-License-Identifier: MPL-2.0
import { useEffect, useState, type FormEvent } from "react";
import { bridge, type AgentSession, type ProjectTarget, type ProviderStatus, type SwitchView } from "./bridge";

const blockers: Record<string, string> = {
  TARGET_UNAVAILABLE: "The selected target provider is unavailable.",
  TARGET_UNSUPPORTED: "The selected target provider lacks the required capabilities.",
  SOURCE_CLEANUP_UNCONFIRMED: "Source process cleanup could not be confirmed.",
  PROJECT_SESSION_ACTIVE: "Another Agent Session is active in this project.",
  WORK_ITEM_CLOSED: "The Work Item is closed.",
  SOURCE_LEASE_EXPIRED: "The source session needs reconciliation.",
};

export function ProviderSwitch({ root, source, providers, disabled, onSession, openTarget, canOpenProjection, canOpenHandoff }: {
  root: string;
  source: AgentSession;
  providers: ProviderStatus[];
  disabled: boolean;
  onSession: (sessionId: string) => void;
  openTarget: (target: ProjectTarget) => void;
  canOpenProjection: boolean;
  canOpenHandoff: boolean;
}) {
  const [target, setTarget] = useState("");
  const [view, setView] = useState<SwitchView | null>(null);
  const [actionBlocker, setActionBlocker] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [replayConfirmed, setReplayConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [inspection, setInspection] = useState(0);
  const providerOptions = providers.filter((row) => row.supported && row.provider !== source.provider);
  const sourceId = source.session_id;
  const generation = source.run_generation;

  useEffect(() => {
    if (!target || pending) return;
    let mounted = true;
    let timer: ReturnType<typeof setTimeout>;
    async function inspect() {
      try {
        const result = await bridge.switchStatus(root, sourceId, target, generation);
        if (!mounted) return;
        setView(result);
        if (result.target_session_id) setActionBlocker(null);
        if (["STOPPING_SOURCE", "TARGET_SESSION_ACTIVE"].includes(result.stage)) {
          timer = setTimeout(() => void inspect(), 1000);
        }
      } catch {
        if (mounted) setError("Could not read the switch status. Check status to retry.");
      }
    }
    void inspect();
    return () => {
      mounted = false;
      clearTimeout(timer);
    };
  }, [root, sourceId, generation, target, pending, inspection]);

  async function execute(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!target || pending || disabled || view?.stage === "STOPPING_SOURCE") return;
    setError("");
    setActionBlocker(null);
    setReplayConfirmed(false);
    setPending(true);
    try {
      const result = await bridge.switchExecute(root, sourceId, target, generation);
      setView(result);
      setActionBlocker(result.blocker);
      setReplayConfirmed(Boolean(view?.target_session_id && result.target_session_id === view.target_session_id));
    } catch {
      setError("Could not confirm the switch. Check status before retrying.");
    } finally {
      setPending(false);
    }
  }

  const blocker = view?.blocker ?? actionBlocker;
  const targetSession = view?.target_session_id;
  const action = targetSession ? "Replay switch" : view &&
    ["SOURCE_TERMINAL", "HANDOFF_READY", "PROJECTION_READY"].includes(view.stage)
    ? "Continue switch" : "Switch provider";

  return (
    <section className="provider-switch" aria-labelledby="provider-switch-title">
      <h5 id="provider-switch-title">Provider switch</h5>
      <p>Continue this Work Item in a new Agent Session using its saved Handoff and Context Projection.</p>
      <dl className="facts">
        <div><dt>Source session</dt><dd><code>{sourceId}</code></dd></div>
        <div><dt>Source provider</dt><dd>{source.provider}</dd></div>
        <div><dt>Source state</dt><dd>{view?.source_state ?? source.state}</dd></div>
        <div><dt>Work Item</dt><dd><code>{view?.work_item_id ?? source.work_item_id}</code></dd></div>
        {(view?.cleanup_unconfirmed ?? source.cleanup_unconfirmed) &&
          <div><dt>Source cleanup</dt><dd>Unconfirmed</dd></div>}
      </dl>
      {providerOptions.length > 0 && (
        <form className="field-row" onSubmit={(event) => void execute(event)}>
          <div>
            <label htmlFor="switch-target-provider">Target provider</label>
            <select id="switch-target-provider" required value={target} disabled={disabled || pending}
              onChange={(event) => { setTarget(event.target.value); setView(null); setActionBlocker(null); setError(""); setReplayConfirmed(false); }}>
              <option value="">Select target provider</option>
              {providerOptions.map(({ provider }) => <option key={provider}>{provider}</option>)}
            </select>
          </div>
          <button type="submit" disabled={disabled || pending || !target || view?.stage === "STOPPING_SOURCE"}>
            {action}
          </button>
          <button type="button" disabled={disabled || pending || !target} onClick={() => {
            setError("");
            setView(null);
            setActionBlocker(null);
            setReplayConfirmed(false);
            setInspection((value) => value + 1);
          }}>Check switch status</button>
        </form>
      )}
      {pending && <p role="status">Requesting provider switch…</p>}
      {replayConfirmed && <p role="status" className="switch-replay-confirmed">Existing target session confirmed.</p>}
      {error && <p role="alert">{error}</p>}
      {blocker && <p role="alert">{blockers[blocker] ?? "The switch is blocked."}</p>}
      {view && (
        <>
          <p role="status" className="switch-stage">{view.stage}</p>
          <dl className="facts switch-continuity">
            {view.handoff_id && <div><dt>Source Handoff</dt><dd><code>{view.handoff_id}</code></dd></div>}
            {view.handoff_digest && <div><dt>Handoff digest</dt><dd><code>{view.handoff_digest}</code></dd></div>}
            {view.projection_id && <div><dt>Target Projection</dt><dd><code>{view.projection_id}</code></dd></div>}
            {view.projection_digest && <div><dt>Projection digest</dt><dd><code>{view.projection_digest}</code></dd></div>}
            {view.target_session_id && <div><dt>Target session</dt><dd><code>{view.target_session_id}</code></dd></div>}
            {view.target_state && <div><dt>Target state</dt><dd>{view.target_state}</dd></div>}
          </dl>
          <div className="field-row">
          {view.handoff_id && canOpenHandoff && <button type="button" className="secondary" disabled={disabled || pending}
            onClick={() => openTarget({ tab: "context", id: view.handoff_id!, handoff: true })}>Open Handoff</button>}
          {view.projection_id && canOpenProjection && <button type="button" className="secondary" disabled={disabled || pending}
            onClick={() => openTarget({ tab: "context", id: view.projection_id! })}>Open Target Projection</button>}
          {targetSession && <button type="button" disabled={disabled || pending}
            onClick={() => onSession(targetSession)}>Open target session</button>}
          </div>
        </>
      )}
    </section>
  );
}
