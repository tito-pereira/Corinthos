// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState, type FormEvent } from "react";
import { bridge, type ProviderStatus } from "./bridge";

const configuration = {
  operational: "Project override",
  initial: "Initial project choice",
  global: "User setting",
  default: "Automatic selection",
};

const observation = {
  OBSERVED: "Previously observed",
  NOT_OBSERVED: "Not yet observed",
  UNKNOWN: "Unknown",
};

export function Providers({ providers }: { providers: ProviderStatus[] }) {
  return providers.length > 0 ? <ul className="providers">
    {providers.map((row) => <li key={row.provider}>
      <div className="provider-heading"><h3>{row.provider}</h3><span>{observation[row.observation]}</span></div>
      <p>{row.supported ? configuration[row.configuration] : "Unsupported provider"}</p>
      {row.executable && <code>{row.executable}</code>}
      {row.capabilities && row.observation === "OBSERVED" && <p>
        {row.capabilities.available ? "Available at last check" : "Unavailable at last check"}
        {row.capabilities.version && ` · ${row.capabilities.version}`}
        {row.capabilities.json_events && " · JSON events"}
        {row.capabilities.stdin_prompt && " · stdin"}
        {row.capabilities.native_resume && " · Resume"}
        {row.capabilities.read_only && " · Read-only"}
      </p>}
    </li>)}
  </ul> : <p>No providers are recorded.</p>;
}

export function ProviderSettings({ methods, root, providers, onProviders }: {
  methods: string[];
  root?: string;
  providers: ProviderStatus[];
  onProviders: (value: ProviderStatus[]) => void;
}) {
  const [project, setProject] = useState(false);
  const scopeRoot = project ? root : undefined;
  const [scoped, setScoped] = useState<ProviderStatus[] | null>(null);
  const [provider, setProvider] = useState("");
  const [executable, setExecutable] = useState("");
  const [busy, setBusy] = useState(false);
  const [reset, setReset] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const request = useRef(0);
  const inFlight = useRef(false);
  const canConfigure = methods.includes("provider.configure");
  const canReset = methods.includes("provider.reset");
  const canRead = methods.includes("settings.providers");

  useEffect(() => {
    if (!scopeRoot || !canRead) return;
    const current = ++request.current;
    bridge.settingsProviders(scopeRoot).then(
      (value) => { if (current === request.current) setScoped(value); },
      () => { if (current === request.current) setError("Could not read the selected project's provider settings."); },
    );
    return () => { request.current += 1; };
  }, [scopeRoot, canRead]);

  async function run(action: () => Promise<void>, message: string, success: string) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    setSaved("");
    try { await action(); setSaved(success); } catch { setError(message); }
    finally { inFlight.current = false; setBusy(false); }
  }

  function change(removing = false) {
    if (!provider || (removing ? !canReset || !reset : !canConfigure || !executable.trim())) return;
    void run(async () => {
      request.current += 1;
      const value = removing
        ? await bridge.providerReset(provider, scopeRoot)
        : await bridge.providerConfigure(provider, executable.trim(), scopeRoot);
      if (scopeRoot) setScoped(value);
      else onProviders(value);
      setExecutable("");
      setReset(false);
    }, "Could not change this provider. Check the executable path and current settings.",
    removing ? "Provider selection reset." : "Provider selection saved.");
  }

  const choices = scopeRoot ? scoped ?? [] : providers;
  return <section aria-labelledby="settings-title">
    <div className="section-heading">
      <h2 id="settings-title">Settings</h2>
      {methods.includes("providers.refresh") && <button type="button" disabled={busy} onClick={() => void run(async () => {
        const value = await bridge.providersRefresh();
        onProviders(value);
        if (scopeRoot && canRead) setScoped(await bridge.settingsProviders(scopeRoot));
      }, "Could not refresh provider observations. Check the configured executables and retry.", "Provider observations refreshed.")}>
        Refresh provider observations
      </button>}
    </div>
    {root && canRead && <label className="checkbox">
      <input type="checkbox" checked={project} disabled={busy} onChange={(event) => {
        setProject(event.target.checked);
        setScoped(null);
        setProvider("");
        setExecutable("");
        setReset(false);
        setSaved("");
        setError("");
      }} /> Configure providers for the selected project
    </label>}
    {scopeRoot && <code>{scopeRoot}</code>}
    {scopeRoot && !scoped && !error ? <p role="status">Reading project provider settings…</p> : <Providers providers={choices} />}
    {error && <p role="alert">{error}</p>}
    {saved && <p role="status">{saved}</p>}
    {(canConfigure || canReset) && <form className="work-form provider-form" onSubmit={(event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      change();
    }}>
      <fieldset disabled={busy || Boolean(scopeRoot && !scoped)}>
        <label htmlFor="settings-provider">Provider</label>
        <select id="settings-provider" required value={provider} onChange={(event) => {
          setProvider(event.target.value);
          setExecutable("");
          setReset(false);
          setSaved("");
        }}>
          <option value="">Select provider</option>
          {choices.filter((row) => row.supported).map((row) => <option key={row.provider}>{row.provider}</option>)}
        </select>
        {canConfigure && <>
          <label htmlFor="settings-executable">Absolute executable path</label>
          <input id="settings-executable" value={executable} maxLength={4096}
            onChange={(event) => setExecutable(event.target.value)} />
          <button type="submit" disabled={!provider || !executable.trim()}>Save provider</button>
        </>}
        {canReset && provider && <>
          <label className="checkbox">
            <input type="checkbox" checked={reset} onChange={(event) => setReset(event.target.checked)} /> Confirm provider reset
          </label>
          <button type="button" disabled={!reset} onClick={() => change(true)}>Reset provider</button>
        </>}
      </fieldset>
    </form>}
  </section>;
}
