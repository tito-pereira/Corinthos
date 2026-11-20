// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState } from "react";
import { bridge, type ProductUpdateStatus } from "./bridge";

const active = new Set<ProductUpdateStatus["phase"]>([
  "CHECKING", "STAGING", "WAITING_FOR_EXIT", "INSTALLING",
]);

export function ProductUpdate({ methods }: { methods: string[] }) {
  const [status, setStatus] = useState<ProductUpdateStatus | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const request = useRef(0);
  const inFlight = useRef(false);
  const mounted = useRef(false);
  const canRead = methods.includes("update.status");

  async function run(action: () => Promise<ProductUpdateStatus>) {
    if (inFlight.current) return;
    inFlight.current = true;
    const current = ++request.current;
    setPending(true);
    setError("");
    try {
      const result = await action();
      if (mounted.current && current === request.current) setStatus(result);
    } catch {
      if (mounted.current && current === request.current) {
        setError("Could not confirm the update status. Read the status before retrying an update.");
      }
    } finally {
      if (mounted.current && current === request.current) {
        inFlight.current = false;
        setPending(false);
      }
    }
  }

  useEffect(() => {
    mounted.current = true;
    if (canRead) void run(() => bridge.updateStatus());
    return () => {
      mounted.current = false;
      inFlight.current = false;
      request.current += 1;
    };
  }, [canRead]);

  useEffect(() => {
    if (!status || !active.has(status.phase) || error) return;
    const timer = setTimeout(() => {
      void run(() => bridge.updateStatus(status.operation_id ?? undefined));
    }, 1000);
    return () => clearTimeout(timer);
  }, [status, error]);

  const working = Boolean(status && active.has(status.phase));
  return <section aria-labelledby="product-update-title">
    <h2 id="product-update-title">Corinthos updates</h2>
    {!canRead ? <p>Product updates are unavailable in this backend.</p> : <>
      {!status && !error && <p role="status">Reading update status…</p>}
      {status && <>
        <dl className="facts">
          <div><dt>Installed version</dt><dd>{status.installed_version}</dd></div>
          <div><dt>Available version</dt><dd>{status.candidate_version ?? (status.phase === "NO_UPDATE" ? "None available" : "Not checked")}</dd></div>
        </dl>
        <p role="status">{status.message}</p>
        {working && <progress aria-label="Update progress" value={status.progress} max={100}>{status.progress}%</progress>}
        {status.error && <p role="alert">{status.error.message}</p>}
      </>}
      {error && <p role="alert">{error}</p>}
      <div className="field-row">
        <button type="button" disabled={pending} onClick={() => void run(() => bridge.updateStatus())}>
          Read update status
        </button>
        {methods.includes("update.check") && <button type="button"
          disabled={pending || working || Boolean(error) || status?.phase === "UNSUPPORTED"}
          onClick={() => void run(() => bridge.updateCheck())}>Check for updates</button>}
        {status?.can_start && status.operation_id && methods.includes("update.start") && <button type="button"
          disabled={pending || Boolean(error)} onClick={() => void run(() => bridge.updateStart(status.operation_id!))}>
          Update Corinthos
        </button>}
        {status?.can_finish && status.operation_id && methods.includes("update.finish") && <button type="button"
          disabled={pending || Boolean(error)} onClick={() => void run(() => bridge.updateFinish(status.operation_id!))}>
          {status.phase === "RECOVERY_REQUIRED" ? "Recover and restart" : "Restart to finish"}
        </button>}
      </div>
      {status?.can_finish && <p>Restarting closes this window and reopens Corinthos when the update or recovery finishes.
        Close other Corinthos windows and commands first.</p>}
    </>}
  </section>;
}
