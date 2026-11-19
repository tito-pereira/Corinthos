// SPDX-License-Identifier: MPL-2.0
import type { BackendSnapshot, ProjectSummary, RuntimeDiagnostics } from "./bridge";
import corinthosLogo from "./assets/branding/corinthos-logo.svg?no-inline";

export type ConnectionState = "connecting" | "connected" | "disconnected" | "restarting" | "unavailable";

export const connectionLabels: Record<ConnectionState, string> = {
  connecting: "Connecting…",
  connected: "Connected",
  disconnected: "Disconnected",
  restarting: "Restarting…",
  unavailable: "Backend unavailable",
};

export function BackendDiagnostics({ snapshot, connection, runtime, project, projectCurrent = true,
  open, pending, error, onToggle }: {
  snapshot: BackendSnapshot | null;
  connection: ConnectionState;
  runtime: RuntimeDiagnostics | null;
  project: ProjectSummary | null;
  projectCurrent?: boolean;
  open: boolean;
  pending: boolean;
  error: string;
  onToggle: (open: boolean) => void;
}) {
  const handshake = snapshot?.connection === "ready" ? snapshot.handshake : null;
  const observed = (value: string | number | undefined) => value === undefined
    ? "Unavailable" : `${value}${connection === "connected" ? "" : " (last observed)"}`;
  const projectSchema = !project ? "No project selected" : !projectCurrent ? "Not yet verified"
    : !project.exists || !project.is_directory ? "Unavailable"
    : project.storage === "ABSENT" ? "No database"
    : project.schema_version === null ? "Unavailable" : `v${project.schema_version}`;

  return <details className="diagnostics" open={open} onToggle={(event) => onToggle(event.currentTarget.open)}>
    <summary>About / Diagnostics</summary>
    {open && <section aria-labelledby="diagnostics-title">
      <img className="brand-logo diagnostics-brand" src={corinthosLogo} alt="" />
      <div className="section-heading"><h2 id="diagnostics-title">Diagnostics</h2></div>
      <dl className="facts">
        <div><dt>Application version</dt><dd>{observed(handshake?.version)}</dd></div>
        <div><dt>Desktop version</dt><dd>{snapshot?.desktop.version ?? "Unavailable"}</dd></div>
        <div><dt>RPC protocol</dt><dd>{observed(handshake ? `v${handshake.protocol_version}` : undefined)}</dd></div>
        <div><dt>Backend state</dt><dd>{connectionLabels[connection]}</dd></div>
        <div><dt>Private runtime</dt><dd>{observed(runtime ? `${runtime.python_implementation} ${runtime.python_version}` : undefined)}</dd></div>
        <div><dt>Supported storage schema</dt><dd>{observed(runtime ? `v${runtime.supported_schema_version}` : undefined)}</dd></div>
        <div><dt>Selected project schema</dt><dd>{connection === "connected" ? projectSchema : project ? `${projectSchema} (last observed)` : projectSchema}</dd></div>
        <div><dt>Desktop identifier</dt><dd>{snapshot?.desktop.identifier ?? "Unavailable"}</dd></div>
      </dl>
      {pending && <p role="status">Reading runtime diagnostics…</p>}
      {error && <p role="alert">{error}</p>}
    </section>}
  </details>;
}
