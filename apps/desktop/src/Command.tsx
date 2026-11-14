// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState } from "react";
import {
  bridge, type CommandAttention, type CommandProjectCard, type CommandValidation, type ContractReference,
  type ProjectTarget,
} from "./bridge";

type OpenTarget = (root: string, target: ProjectTarget) => void;

export function Badge({ value }: { value: string }) {
  return <span className="status-badge" data-status={value}>{value}</span>;
}

function Reference({ reference }: { reference: ContractReference }) {
  return <code>{reference.contract_id} · {reference.revision_id}</code>;
}

function attentionTarget(item: CommandAttention): ProjectTarget {
  const tab = item.navigation.toLowerCase() as ProjectTarget["tab"];
  const review = item.owner_type === "PROJECT_PLANNING" ? "PROJECT"
    : item.owner_type === "WORKFLOW_PLANNING" ? "WORKFLOW" : undefined;
  const id = item.owner_type === "WORK_ITEM" || item.owner_type === "SESSION" ||
    item.owner_type === "CONTEXT_PROJECTION" || review ? item.owner_id ?? undefined : undefined;
  return { tab, id, review };
}

export function Attention({ card, open }: { card: CommandProjectCard; open: OpenTarget }) {
  return card.attention.length > 0 && <ul className="command-attention">
    {card.attention.map((item) => <li key={[item.kind, item.reason_code, item.owner_id].join(":")}>
      <div><Badge value={item.severity} /> <strong>{item.summary}</strong><code>{item.reason_code}</code></div>
      <button type="button" className="secondary" onClick={() => open(card.root, attentionTarget(item))}>Open Attention</button>
    </li>)}
  </ul>;
}

export function ValidationFacts({ validation }: { validation: CommandValidation }) {
  const run = validation.latest_run;
  return <>
    <p><Badge value={validation.state} /></p>
    <dl className="facts">
      {run && <>
        <div><dt>Run</dt><dd><code>{run.run_id}</code></dd></div>
        <div><dt>Run state</dt><dd>{run.state}</dd></div>
        <div><dt>Run plan</dt><dd><Reference reference={run.plan} /></dd></div>
        <div><dt>Plan current</dt><dd>{run.plan_is_active ? "Yes" : "No"}</dd></div>
        {run.result && <div><dt>Result</dt><dd><Reference reference={run.result} /></dd></div>}
      </>}
      {validation.outcome && <div><dt>Outcome</dt><dd>{validation.outcome}</dd></div>}
    </dl>
    {validation.active_plan_refs.length > 0 && <div>
      <h4>Active Validation plans</h4>
      <ul>{validation.active_plan_refs.map((plan) => <li key={plan.revision_id}><Reference reference={plan} /></li>)}</ul>
    </div>}
    {!run && <p>No Validation run recorded.</p>}
  </>;
}

function CommandCard({ card, open, methods }: { card: CommandProjectCard; open: OpenTarget; methods: string[] }) {
  const active = card.sessions?.active;
  const latest = active ?? card.sessions?.latest_terminal;
  const canSwitch = Boolean(active && !active.cleanup_unconfirmed &&
    methods.includes("switch.status") && methods.includes("switch.execute") && methods.includes("session.get"));
  return <article className="command-card">
    <div className="section-heading">
      <h3>{card.display_name}</h3>
      <Badge value={card.availability} />
    </div>
    <code className="command-card-root">{card.root}</code>
    {card.error && <p role="alert">Could not inspect project state. <code>{card.error}</code></p>}
    <dl className="facts">
      <div><dt>Availability</dt><dd>{card.availability}</dd></div>
      <div><dt>Storage</dt><dd>{card.storage}{card.schema_version !== null && ` · v${card.schema_version}`}</dd></div>
      {card.project_id && <div><dt>Project ID</dt><dd><code>{card.project_id}</code></dd></div>}
      {card.import_status && <div><dt>Import status</dt><dd><Badge value={card.import_status} /></dd></div>}
      {card.external_import && <div><dt>Import snapshot</dt><dd><code>{card.external_import.snapshot_digest}</code></dd></div>}
      {card.work_counts && <div><dt>Work Items</dt><dd>{card.work_counts.by_status.COMPLETED ?? 0} / {card.work_counts.total} completed</dd></div>}
      {card.work_counts && <div><dt>Work Item scopes</dt><dd>{Object.entries(card.work_counts.by_scope).map(([name, count]) => `${name} ${count}`).join(" · ")}</dd></div>}
      {card.work_counts && <div><dt>Work Item statuses</dt><dd>{Object.entries(card.work_counts.by_status).map(([name, count]) => `${name} ${count}`).join(" · ")}</dd></div>}
      {active && <div><dt>Active session</dt><dd><code>{active.session_id}</code></dd></div>}
      {latest && <>
        <div><dt>Session state</dt><dd><Badge value={latest.state} /></dd></div>
        <div><dt>Provider</dt><dd>{latest.provider}</dd></div>
        {latest.cleanup_unconfirmed && <div><dt>Cleanup</dt><dd>Unconfirmed</dd></div>}
        {(latest.input_tokens !== null || latest.output_tokens !== null || latest.cached_tokens !== null) && <div>
          <dt>Reported tokens</dt><dd>{[
            latest.input_tokens !== null && `${latest.input_tokens} input`,
            latest.output_tokens !== null && `${latest.output_tokens} output`,
            latest.cached_tokens !== null && `${latest.cached_tokens} cached`,
          ].filter(Boolean).join(" · ")}</dd>
        </div>}
        {latest.reported_cost !== null && <div><dt>Reported cost</dt><dd>{latest.reported_cost} {latest.reported_cost_currency}</dd></div>}
      </>}
      <div><dt>Validation</dt><dd><Badge value={card.validation.state} /></dd></div>
      {card.latest_context && <>
        <div><dt>Latest projection</dt><dd><code>{card.latest_context.projection_id}</code></dd></div>
        <div><dt>Projection digest</dt><dd><code>{card.latest_context.projection_digest}</code></dd></div>
      </>}
    </dl>
    <div className="field-row">
      {methods.includes("project.summary") && <button type="button" className="secondary" onClick={() => open(
        card.root, { tab: "overview" },
      )}>{card.availability === "AVAILABLE" ? "Open Project" : "Inspect Project"}</button>}
      {latest && methods.includes("session.get") && <button type="button" className="secondary" onClick={() => open(
        card.root, { tab: "agents", id: latest.session_id },
      )}>Open Agent Session</button>}
      {canSwitch && active && <button type="button" onClick={() => open(
        card.root, { tab: "agents", id: active.session_id },
      )}>Switch Provider</button>}
    </div>
    {card.focus_work_items.length > 0 && <ul className="command-focus">
      {card.focus_work_items.map((work) => <li key={work.work_item_id}>
        <div>
          <strong>{work.objective}</strong><code>{work.work_item_id}</code>
          <p><Badge value={work.scope_level} /> <Badge value={work.status} /> {work.stage && <Badge value={work.stage} />}</p>
          <p>Validation <Badge value={work.validation_state} /></p>
          {work.blockers.map((blocker) => <p key={blocker}>{blocker}</p>)}
          {work.next_actions.map((action) => <p key={action}>{action}</p>)}
          {work.latest_projection && <code>{work.latest_projection.projection_digest}</code>}
          {work.provider_continuity && <p>{work.provider_continuity.source_provider} → {work.provider_continuity.target_provider}</p>}
        </div>
        {methods.includes("work.get") && <button type="button" className="secondary"
          onClick={() => open(card.root, { tab: "work", id: work.work_item_id })}>Open Work Item</button>}
      </li>)}
    </ul>}
    <Attention card={card} open={open} />
  </article>;
}

export function Command({ methods, open }: { methods: string[]; open: OpenTarget }) {
  const [page, setPage] = useState<Awaited<ReturnType<typeof bridge.commandOverview>> | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refreshCounter, setRefreshCounter] = useState(0);
  const [observedAt, setObservedAt] = useState<Date | null>(null);
  const request = useRef(0);
  const polling = useRef(false);
  const supported = methods.includes("command.overview");

  useEffect(() => {
    if (!supported) return;
    let mounted = true;
    async function refresh(passive = false) {
      if (passive && (document.hidden || polling.current)) return;
      const id = ++request.current;
      polling.current = true;
      if (!passive) setBusy(true);
      try {
        const result = await bridge.commandOverview(offset);
        if (mounted && id === request.current) {
          setPage(result);
          setObservedAt(new Date());
          setError("");
        }
      } catch {
        if (mounted && id === request.current) setError("Could not read Command. Retry the read.");
      } finally {
        if (id === request.current) {
          polling.current = false;
          if (mounted) setBusy(false);
        }
      }
    }
    void refresh();
    const interval = setInterval(() => void refresh(true), 4000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [supported, offset, refreshCounter]);

  return <section aria-labelledby="command-title">
    <div className="section-heading">
      <h2 id="command-title">Command</h2>
      {supported && <button type="button" disabled={busy} onClick={() => setRefreshCounter((value) => value + 1)}>Refresh Command</button>}
    </div>
    {!supported && <p>Command is unavailable in this backend.</p>}
    {busy && !page && <p role="status">Loading projects…</p>}
    {error && <p role="alert">{error}</p>}
    {observedAt && <p className="command-read">Last read <time className="command-observed"
      dateTime={observedAt.toISOString()}>{observedAt.toLocaleTimeString()}</time></p>}
    {page?.projects.length === 0 && <p>No projects on this page.</p>}
    <div className="command-grid">
      {page?.projects.map((card) => <CommandCard key={card.root} card={card} open={open} methods={methods} />)}
    </div>
    <div className="field-row">
      {offset > 0 && <button type="button" disabled={busy} onClick={() => setOffset(0)}>First Command projects</button>}
      {page?.next_offset != null && <button type="button" disabled={busy}
        onClick={() => setOffset(page.next_offset!)}>More Command projects</button>}
    </div>
  </section>;
}
