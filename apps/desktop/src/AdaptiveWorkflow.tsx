// SPDX-License-Identifier: MPL-2.0
import { useEffect, useState, type FormEvent } from "react";
import {
  bridge, workScopes, type FlowView, type ProjectTarget, type ProviderStatus, type WorkItem, type WorkReport, type WorkScope,
} from "./bridge";

type Props = {
  root: string;
  item: WorkItem;
  methods: string[];
  providers: ProviderStatus[];
  disabled: boolean;
  run: (action: () => Promise<void>, message: string) => Promise<void>;
  onChanged: () => Promise<void>;
  openTarget: (target: ProjectTarget) => void;
};

function Facts({ values }: { values: Record<string, string | null> }) {
  return <dl className="facts">
    {Object.entries(values).map(([name, value]) => <div key={name}>
      <dt>{name}</dt><dd>{value ?? "None recorded"}</dd>
    </div>)}
  </dl>;
}

function Findings({ title, entries }: { title: string; entries: string[] }) {
  return entries.length > 0 && <>
    <h5>{title}</h5>
    <ul>{entries.map((entry, index) => <li key={index}>{entry}</li>)}</ul>
  </>;
}

export function AdaptiveWorkflow({ root, item, methods, providers, disabled, run, onChanged, openTarget }: Props) {
  const [flow, setFlow] = useState<FlowView | null>(null);
  const [error, setError] = useState("");
  const [scope, setScope] = useState<WorkScope | "">("");
  const [confirmation, setConfirmation] = useState(false);
  const [provider, setProvider] = useState("");
  const [readOnly, setReadOnly] = useState(false);
  const [report, setReport] = useState<WorkReport | null>(null);
  const { work_item_id: workId, updated_at: updatedAt } = item;

  useEffect(() => {
    let active = true;
    setFlow(null);
    setReport(null);
    setError("");
    bridge.flowStatus(root, workId).then(
      (status) => { if (active) setFlow(status); },
      () => { if (active) setError("Could not load the adaptive workflow. Refresh to retry."); },
    );
    return () => { active = false; };
  }, [root, workId, updatedAt]);

  async function refresh() {
    setError("");
    setReport(null);
    setFlow(await bridge.flowStatus(root, workId));
  }

  const selectedScope = flow?.selected_scope ?? item.scope_level;
  const rank = workScopes.indexOf(selectedScope as WorkScope);
  const minimumRank = flow ? workScopes.indexOf(flow.minimum_required_scope) : rank;
  const downgrade = Boolean(scope && workScopes.indexOf(scope) < rank);
  const scopeOptions = workScopes.filter((_choice, index) => index !== rank && (index > rank || index >= minimumRank));
  const providerOptions = providers.filter((entry) => entry.supported);
  const terminal = ["COMPLETED", "CANCELLED"].includes(item.status);
  const scopeChangeAllowed = scope && scopeOptions.includes(scope) && (!downgrade || confirmation);

  async function changeScope(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!scope || !scopeChangeAllowed || disabled || terminal) return;
    await run(async () => {
      await bridge.scopeSet(root, item, scope, confirmation);
      setScope("");
      setConfirmation(false);
      await onChanged();
    }, "Could not change scope. Refresh the Work Item before retrying.");
  }

  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!flow?.can_start || !provider || disabled) return;
    await run(async () => {
      setReport(null);
      setFlow(await bridge.flowStart(root, workId, { provider, timeout_seconds: 120, read_only: readOnly }));
    }, "Could not confirm the adaptive start. Refresh the workflow before retrying.");
  }

  return <section className="adaptive-workflow" aria-labelledby="adaptive-workflow-title">
    <div className="section-heading">
      <h4 id="adaptive-workflow-title">Adaptive workflow</h4>
      <button type="button" disabled={disabled} onClick={() => void run(
        refresh, "Could not refresh the adaptive workflow.",
      )}>Refresh workflow</button>
    </div>
    {error ? <p role="alert">{error}</p> : !flow && <p role="status">Loading workflow…</p>}
    {flow && <>
      <Facts values={{
        "Selected scope": flow.selected_scope,
        "Recommended scope": flow.recommended_scope,
        "Minimum required scope": flow.minimum_required_scope,
        "Effective flow": flow.flow_kind,
        Stage: flow.stage,
        Validation: flow.validation_state,
        "Validation policy": flow.validation_policy,
        "Report status": flow.report_status,
        "Workflow revision": flow.workflow_ref?.revision_id ?? null,
      }} />
      <Findings title="Scope signals" entries={flow.signals} />
      <Findings title="Unknowns" entries={flow.unknowns} />
      <Findings title="Gates" entries={flow.gates.map((gate) => `${gate.id} · ${gate.state}`)} />
      <Findings title="Blockers" entries={flow.blockers} />
      <Findings title="Next actions" entries={flow.next_actions} />
      {flow.execution && <Facts values={{
        "Workflow execution": flow.execution.execution_id,
        "Execution state": flow.execution.state,
      }} />}
      <Facts values={{
        "Latest session": flow.latest_session?.session_id ?? null,
        "Session state": flow.latest_session?.state ?? null,
        "Session provider": flow.latest_session?.provider ?? null,
        "Latest projection": flow.latest_projection?.projection_id ?? null,
        "Projection digest": flow.latest_projection?.projection_digest ?? null,
        "Latest Handoff": flow.latest_handoff?.handoff_id ?? null,
      }} />
      {flow.provider_continuity && <Facts values={{
        "Previous provider": flow.provider_continuity.source_provider,
        "Continuation provider": flow.provider_continuity.target_provider,
        "Continuity Handoff": flow.provider_continuity.handoff_id,
      }} />}

      {methods.includes("scope.set") && !terminal && scopeOptions.length > 0 && (
        <form className="field-row" onSubmit={(event) => void changeScope(event)}>
          <div>
            <label htmlFor="adaptive-scope">Change scope</label>
            <select id="adaptive-scope" value={scope} required disabled={disabled} onChange={(event) => {
              setScope(event.target.value as WorkScope | "");
              setConfirmation(false);
            }}>
              <option value="">Select scope</option>
              {scopeOptions.map((choice) => <option key={choice}>{choice}</option>)}
            </select>
          </div>
          {downgrade && <label className="checkbox">
            <input type="checkbox" checked={confirmation} disabled={disabled}
              onChange={(event) => setConfirmation(event.target.checked)} /> Confirm scope downgrade
          </label>}
          <button type="submit" disabled={disabled || !scopeChangeAllowed}>Update scope</button>
        </form>
      )}
      {flow.can_start && methods.includes("flow.start") && providerOptions.length > 0 && (
        <form className="field-row" onSubmit={(event) => void start(event)}>
          <div>
            <label htmlFor="adaptive-provider">Execution provider</label>
            <select id="adaptive-provider" value={provider} required disabled={disabled}
              onChange={(event) => setProvider(event.target.value)}>
              <option value="">Select provider</option>
              {providerOptions.map((choice) => <option key={choice.provider}>{choice.provider}</option>)}
            </select>
          </div>
          <label className="checkbox">
            <input type="checkbox" checked={readOnly} disabled={disabled}
              onChange={(event) => setReadOnly(event.target.checked)} /> Read-only execution
          </label>
          <button type="submit" disabled={disabled || !provider}>Start adaptive workflow</button>
        </form>
      )}
      <div className="field-row">
        {flow.latest_session && methods.includes("session.get") && <button type="button" className="secondary" disabled={disabled}
          onClick={() => openTarget({ tab: "agents", id: flow.latest_session!.session_id })}>Open Agent Session</button>}
        {flow.latest_projection && methods.includes("context.get") && <button type="button" className="secondary" disabled={disabled}
          onClick={() => openTarget({ tab: "context", id: flow.latest_projection!.projection_id })}>Open Context Projection</button>}
        {methods.includes("command.project") && <button type="button" className="secondary" disabled={disabled}
          onClick={() => openTarget({ tab: "validation", id: workId })}>Open Validation</button>}
        {methods.includes("work.report") && <button type="button" disabled={disabled} onClick={() => void run(
          async () => setReport(await bridge.workReport(root, workId)), "Could not load the Work Item report.",
        )}>Show Work Item report</button>}
        {flow.can_complete && methods.includes("flow.complete") && <button type="button" disabled={disabled}
          onClick={() => void run(async () => {
            setFlow(await bridge.flowComplete(root, item));
            await onChanged();
          }, "Could not confirm adaptive completion. Refresh before retrying.")}>Complete adaptive workflow</button>}
      </div>
      {report && <section className="adaptive-report" aria-labelledby="adaptive-report-title">
        <h5 id="adaptive-report-title">Work Item report</h5>
        <p>{report.objective}</p>
        <Facts values={{ "Overall status": report.overall_status, Validation: report.flow.validation_state }} />
        <Findings title="Acceptance criteria" entries={report.acceptance_criteria} />
      </section>}
    </>}
  </section>;
}
