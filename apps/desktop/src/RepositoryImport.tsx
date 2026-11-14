// SPDX-License-Identifier: MPL-2.0
import { useRef, useState, type FormEvent } from "react";
import { bridge, type ImportDiscovery, type ImportResult } from "./bridge";

const importErrors: Record<string, string> = {
  REPOSITORY_CHANGED: "The repository changed after inspection. Inspect it again before importing.",
  CONFIRMATION_REQUIRED: "The repository requires additional confirmations. Inspect it again to review them.",
  ALREADY_MANAGED: "Corinthos already manages this repository. Open the existing project.",
  INVALID: "Corinthos could not verify the existing private state.",
  INCOMPLETE: "The previous import is incomplete. Inspect the repository before retrying.",
  REGISTRY_UNAVAILABLE: "The import was confirmed, but Projects could not save the entry.",
};

function Observations({ label, values }: { label: string; values: string[] }) {
  return <div>
    <h4>{label}</h4>
    {values.length ? <ul>{values.map((value, index) => <li key={index}>{value}</li>)}</ul> : <p>None reported.</p>}
  </div>;
}

export function RepositoryImport({ canCommit, canCheckStatus, canOpen, disabled, onOpen }: {
  canCommit: boolean;
  canCheckStatus: boolean;
  canOpen: boolean;
  disabled: boolean;
  onOpen: (root: string) => Promise<void>;
}) {
  const [path, setPath] = useState("");
  const [preview, setPreview] = useState<ImportDiscovery | null>(null);
  const [confirmations, setConfirmations] = useState<string[]>([]);
  const [result, setResult] = useState<Extract<ImportResult, { status: "IMPORTED" }> | null>(null);
  const [existing, setExisting] = useState<ImportResult["status"] | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const busy = disabled || pending;
  const confirmed = preview?.ambiguities.every((entry) => !entry.required || confirmations.includes(entry.code));
  const canConfirmImport = preview?.commit_allowed && canCommit && !result &&
    (!preview.existing_state || existing === "INCOMPLETE");
  const canOpenExisting = canOpen && (existing === "IMPORTED" || existing === "ALREADY_MANAGED");

  function clearReview() {
    setPreview(null);
    setResult(null);
    setExisting(null);
    setConfirmations([]);
    setError("");
  }

  async function inspect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || submitting.current || !path.trim()) return;
    submitting.current = true;
    clearReview();
    setPending(true);
    try {
      const discovered = await bridge.importInspect(path.trim());
      setPreview(discovered);
      if (discovered.existing_state && canCheckStatus) {
        setExisting((await bridge.importStatus(discovered.root)).status);
      }
    } catch {
      setError("Could not inspect the repository. Check that the path is a supported local Git repository.");
    } finally {
      submitting.current = false;
      setPending(false);
    }
  }

  async function commit() {
    if (!preview || !canConfirmImport || busy || submitting.current || !confirmed) return;
    submitting.current = true;
    setError("");
    setPending(true);
    try {
      const imported = await bridge.importCommit(preview.root, preview.discovery_digest, confirmations.slice().sort());
      if (imported.status !== "IMPORTED") {
        setPreview(null);
        setConfirmations([]);
        setError(importErrors[imported.error ?? imported.status] ?? "The import was rejected. Inspect the repository again.");
        return;
      }
      setResult(imported);
      if (imported.error === "REGISTRY_UNAVAILABLE") {
        setError(importErrors.REGISTRY_UNAVAILABLE);
      } else {
        await onOpen(imported.root);
      }
    } catch {
      setPreview(null);
      setConfirmations([]);
      setError("Could not confirm the import. Inspect the repository again before retrying.");
    } finally {
      submitting.current = false;
      setPending(false);
    }
  }

  return <section className="repository-import" aria-labelledby="repository-import-title">
    <h3 id="repository-import-title">Import existing repository</h3>
    <form onSubmit={(event) => void inspect(event)}>
      <label htmlFor="import-root">Local repository path</label>
      <div className="field-row">
        <input id="import-root" value={path} maxLength={4096} required disabled={busy} onChange={(event) => {
          setPath(event.target.value);
          clearReview();
        }} />
        <button type="submit" disabled={busy || !path.trim()}>Inspect repository</button>
      </div>
    </form>
    {pending && <p role="status">Working on repository import…</p>}
    {error && <p role="alert">{error}</p>}
    {preview && <div className="import-preview">
      <h4>DISCOVERED repository facts</h4>
      <dl className="facts">
        <div><dt>Git root</dt><dd><code>{preview.root}</code></dd></div>
        <div><dt>HEAD</dt><dd><code>{preview.head ?? "None"}</code></dd></div>
        <div><dt>Branch</dt><dd>{preview.branch ?? "Detached"}</dd></div>
        <div><dt>Working tree</dt><dd>{preview.dirty ? "Dirty" : "Clean"}</dd></div>
        <div><dt>Tracked files</dt><dd>{preview.tracked_count}</dd></div>
        <div><dt>Discovery digest</dt><dd><code>{preview.discovery_digest}</code></dd></div>
      </dl>
      <Observations label="Languages" values={Object.entries(preview.languages).map(([name, count]) => `${name} · ${count}`)} />
      <Observations label="Manifests" values={preview.manifests.map((entry) => `${entry.path} · ${entry.ecosystem}`)} />
      <Observations label="Documentation" values={preview.docs} />
      <Observations label="Tests" values={preview.tests} />
      <Observations label="DISCOVERED module candidates" values={preview.candidates.map((entry) => `${entry.path} · ${entry.ecosystem}`)} />
      <Observations label="Exclusions" values={preview.exclusions} />
      <h4>Ambiguities</h4>
      {preview.ambiguities.length === 0 && <p>None reported.</p>}
      {preview.ambiguities.map((entry) => <div key={entry.code}>
        <p><strong>{entry.code}</strong> {entry.explanation}</p>
        {entry.options.length > 0 && <p>Options: {entry.options.join(", ")}</p>}
        {entry.required && canConfirmImport && <label className="checkbox">
          <input type="checkbox" checked={confirmations.includes(entry.code)} disabled={busy || Boolean(result)}
            onChange={(event) => {
              const checked = event.target.checked;
              setConfirmations((previous) => checked ? [...previous, entry.code] : previous.filter((code) => code !== entry.code));
            }} />
          Confirm {entry.code}
        </label>}
      </div>)}
      {preview.existing_state && <p>{canOpenExisting
        ? "Corinthos already manages this repository."
        : existing === "INCOMPLETE"
          ? "The previous import is incomplete. Confirm the exact discovery to retry."
          : "Existing Corinthos state could not be confirmed."}</p>}
      {!preview.existing_state && !preview.commit_allowed && <p>This discovery cannot be imported.</p>}
      {canOpenExisting && <button type="button" disabled={busy}
        onClick={() => void onOpen(preview.root)}>Open existing project</button>}
      {canConfirmImport && <button type="button" disabled={busy || !confirmed} onClick={() => void commit()}>
        Import repository
      </button>}
    </div>}
    {result && <p role="status" className="import-confirmed">
      CONFIRMED import <code>{result.snapshot_digest}</code>
    </p>}
    {result?.error === "REGISTRY_UNAVAILABLE" && canOpen && <button type="button" disabled={busy}
      onClick={() => void onOpen(result.root)}>Retry opening imported project</button>}
  </section>;
}
