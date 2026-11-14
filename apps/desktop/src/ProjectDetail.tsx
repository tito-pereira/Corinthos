// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState } from "react";
import { Attention, Badge, ValidationFacts } from "./Command";
import { bridge, type CommandProjectCard, type ContractReference, type FlowView, type ProjectTarget } from "./bridge";
import { ProjectReviews } from "./ProjectReviews";

type InspectProps = {
  root: string;
  methods: string[];
  target: ProjectTarget;
  open: (root: string, target: ProjectTarget) => void;
};

function useProjectCard(root: string, supported: boolean) {
  const [card, setCard] = useState<CommandProjectCard | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const request = useRef(0);
  useEffect(() => {
    if (!supported) return;
    const current = ++request.current;
    bridge.commandProject(root).then(
      (result) => {
        if (current !== request.current) return;
        setCard(result);
        setError("");
      },
      () => {
        if (current === request.current) setError("Could not inspect this project. Retry the read.");
      },
    );
    return () => { request.current += 1; };
  }, [root, supported, revision]);
  const refresh = () => {
    setError("");
    setRevision((previous) => previous + 1);
  };
  return { card, error, refresh };
}

export function ProjectOverview({ root, methods, target, open }: InspectProps) {
  const supported = methods.includes("command.project");
  const { card, error, refresh } = useProjectCard(root, supported);
  const session = card?.sessions?.active ?? card?.sessions?.latest_terminal;
  return <div className="project-overview">
    {supported && <div className="section-heading">
      <h3>Overview</h3>
      <button type="button" className="secondary" onClick={refresh}>Refresh overview</button>
    </div>}
    {error && <p role="alert">{error}</p>}
    {card && <>
      {card.error && <p role="alert">Project state is unavailable. <code>{card.error}</code></p>}
      <dl className="facts">
        <div><dt>Availability</dt><dd><Badge value={card.availability} /></dd></div>
        {card.project_id && <div><dt>Project ID</dt><dd><code>{card.project_id}</code></dd></div>}
        {card.import_status && <div><dt>Import status</dt><dd><Badge value={card.import_status} /></dd></div>}
        {card.external_import && <>
          <div><dt>Import snapshot</dt><dd><code>{card.external_import.snapshot_digest}</code></dd></div>
          <div><dt>Git object format</dt><dd>{card.external_import.summary.object_format === "sha256" ? "SHA-256" : "SHA-1"}</dd></div>
          {card.external_import.summary.tracked_count != null &&
            <div><dt>Tracked files at import</dt><dd>{card.external_import.summary.tracked_count}</dd></div>}
        </>}
        {session && <div><dt>Agent Session</dt><dd>{session.provider} · <Badge value={session.state} /></dd></div>}
        <div><dt>Validation</dt><dd><Badge value={card.validation.state} /></dd></div>
        {card.lifecycle && <div><dt>Lifecycle tracker</dt><dd><code>{card.lifecycle.tracker_id}</code></dd></div>}
        {card.lifecycle?.active_build_id && <div><dt>Active build</dt><dd><code>{card.lifecycle.active_build_id}</code></dd></div>}
        {card.latest_context && <div><dt>Latest Context digest</dt><dd><code>{card.latest_context.projection_digest}</code></dd></div>}
      </dl>
      <div className="field-row">
        {session && methods.includes("session.get") && <button type="button" className="secondary"
          onClick={() => open(root, { tab: "agents", id: session.session_id })}>Open Agent Session</button>}
        {card.latest_context && methods.includes("context.get") && <button type="button" className="secondary"
          onClick={() => open(root, { tab: "context", id: card.latest_context!.projection_id })}>Open Context Projection</button>}
      </div>
      <Attention card={card} open={open} />
    </>}
    {methods.includes("review.list") && methods.includes("review.get") && <ProjectReviews
      key={target.id ?? "all"} root={root} methods={methods} initialId={target.id} initialKind={target.review} />}
  </div>;
}

function ValidationReferences({ refs }: { refs: ContractReference[] }) {
  return refs.length > 0 && <ul>{refs.map((reference) => <li key={reference.revision_id}>
    <code>{reference.contract_id} · {reference.revision_id}</code>
  </li>)}</ul>;
}

export function ProjectValidation({ root, methods, target, open }: InspectProps) {
  const { card, error, refresh } = useProjectCard(root, methods.includes("command.project"));
  const [flow, setFlow] = useState<FlowView | null>(null);
  const [workError, setWorkError] = useState("");
  const workId = target.id;
  const canInspectWork = methods.includes("flow.status");
  useEffect(() => {
    if (!workId || !canInspectWork) return;
    let current = true;
    bridge.flowStatus(root, workId).then(
      (result) => { if (current) setFlow(result); },
      () => { if (current) setWorkError("Could not read adaptive Validation for this Work Item."); },
    );
    return () => { current = false; };
  }, [root, workId, canInspectWork]);
  const exactWork = flow?.work_item_id === workId ? flow : null;
  const attention = card?.attention.filter((item) => item.navigation === "VALIDATION") ?? [];
  return <div className="validation-view">
    <div className="section-heading">
      <h3>Validation</h3>
      <button type="button" className="secondary" onClick={refresh}>Refresh Validation</button>
    </div>
    {error && <p role="alert">{error}</p>}
    {card?.error && <p role="alert">Project Validation state is unavailable. <code>{card.error}</code></p>}
    {card && <ValidationFacts validation={card.validation} />}
    {workError && <p role="alert">{workError}</p>}
    {exactWork && <div className="work-selection">
      <h4>Work Item Validation</h4>
      <code>{exactWork.work_item_id}</code><Badge value={exactWork.validation_state} />
      <ValidationReferences refs={exactWork.validation_result_refs ?? []} />
      <ul>{exactWork.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul>
      <button type="button" className="secondary" onClick={() => open(root, { tab: "work", id: exactWork.work_item_id })}>Open Work Item</button>
    </div>}
    {card && attention.length > 0 && <Attention card={{ ...card, attention }} open={open} />}
    <p>Run and recover independent checks with the existing <code>corinthos validation</code> commands.</p>
  </div>;
}
