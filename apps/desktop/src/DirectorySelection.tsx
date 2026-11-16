// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState } from "react";
import { bridge } from "./bridge";

let pickerInFlight = false;

export function DirectorySelection({ id, label, value, context, disabled, invalid, describedBy, onChange, onPickingChange }: {
  id: string;
  label: string;
  value: string;
  context: string;
  disabled: boolean;
  invalid?: boolean;
  describedBy?: string;
  onChange: (path: string) => void;
  onPickingChange: (pending: boolean) => void;
}) {
  const [picking, setPicking] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const mounted = useRef(true);
  const latest = useRef({ value, context });
  latest.current = { value, context };

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; generation.current += 1; };
  }, []);

  useEffect(() => {
    generation.current += 1;
    setError("");
  }, [value, context]);

  async function select() {
    if (disabled || pickerInFlight) return;
    pickerInFlight = true;
    const request = ++generation.current;
    const selectedFor = { value, context };
    const current = () => mounted.current && generation.current === request &&
      latest.current.value === selectedFor.value && latest.current.context === selectedFor.context;
    setPicking(true);
    setError("");
    onPickingChange(true);
    try {
      const selected = await bridge.selectProjectDirectory();
      if (selected !== null && selected !== selectedFor.value && current()) onChange(selected);
    } catch {
      if (current()) setError("Folder selection is unavailable or failed. Enter the path manually.");
    } finally {
      pickerInFlight = false;
      if (mounted.current) setPicking(false);
      onPickingChange(false);
    }
  }

  return <div className="directory-selection">
    <div className="field-row">
      <button type="button" aria-label={`Select folder for ${label.toLowerCase()}`}
        disabled={disabled || picking} onClick={() => void select()}>Select folder</button>
      <span>{value ? <code>{value}</code> : "No folder selected"}</span>
    </div>
    {picking && <p role="status">Selecting folder…</p>}
    {error && <p role="alert">{error}</p>}
    <details>
      <summary>Enter path manually</summary>
      <label htmlFor={id}>{label}</label>
      <input id={id} value={value} maxLength={4096} required disabled={disabled || picking}
        placeholder="Enter an absolute local directory path" aria-invalid={invalid}
        aria-describedby={describedBy} onChange={(event) => onChange(event.target.value)} />
    </details>
  </div>;
}
