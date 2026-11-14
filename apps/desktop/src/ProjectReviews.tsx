// SPDX-License-Identifier: MPL-2.0
import { useEffect, useRef, useState } from "react";
import { bridge, type ReviewDetails, type ReviewSummary } from "./bridge";

export function ProjectReviews({ root, methods, initialId, initialKind }: {
  root: string;
  methods: string[];
  initialId?: string;
  initialKind?: ReviewSummary["kind"];
}) {
  const [page, setPage] = useState<Awaited<ReturnType<typeof bridge.reviews>> | null>(null);
  const [detail, setDetail] = useState<ReviewDetails | null>(null);
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [deferred, setDeferred] = useState(false);
  const [confirmation, setConfirmation] = useState(false);
  const inFlight = useRef(false);

  useEffect(() => {
    let current = true;
    bridge.reviews(root, 0).then(
      (result) => {
        if (!current) return;
        setPage(result);
        const kind = initialKind ?? result.reviews.find((review) => review.session_id === initialId)?.kind;
        if (!initialId || !kind) return;
        bridge.reviewGet(root, kind, initialId).then(
          (value) => { if (current) setDetail(value); },
          () => { if (current) setError("Could not open the exact review. Refresh pending decisions."); },
        );
      },
      () => { if (current) setError("Could not read pending decisions."); },
    );
    return () => { current = false; };
  }, [root, initialId, initialKind]);

  async function run(action: () => Promise<void>, message: string) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try { await action(); } catch { setError(message); }
    finally { inFlight.current = false; setBusy(false); }
  }

  function load(target: number) {
    return run(async () => {
      setPage(await bridge.reviews(root, target));
      setDetail(null);
      setConfirmation(false);
      setDeferred(false);
      setOffset(target);
    }, "Could not refresh pending decisions.");
  }

  const digest = detail?.review.digest;
  return <div className="project-reviews">
    <div className="section-heading">
      <h3>Pending decisions</h3>
      <button type="button" className="secondary" disabled={busy} onClick={() => void load(offset)}>Refresh decisions</button>
    </div>
    {error && <p role="alert">{error}</p>}
    {page?.reviews.length === 0 && <p>No pending decisions on this page.</p>}
    {deferred && <p role="status">Decision deferred.</p>}
    {page && <ul className="work-tree">
      {page.reviews.map((review) => <li key={review.kind + review.session_id}>
        <button type="button" className="secondary" disabled={busy} onClick={() => void run(async () => {
          setConfirmation(false);
          setDeferred(false);
          setDetail(await bridge.reviewGet(root, review.kind, review.session_id));
        }, "Could not read this exact review.")}>Review {review.kind.toLowerCase()} decision</button>
        <code>{review.session_id}</code><span>{review.state}</span>
      </li>)}
    </ul>}
    <div className="field-row">
      {offset > 0 && <button type="button" disabled={busy} onClick={() => void load(0)}>First decisions</button>}
      {page?.next_offset != null && <button type="button" disabled={busy} onClick={() => void load(page.next_offset!)}>More decisions</button>}
    </div>
    {detail && <div className="work-selection review-detail">
      <h4>{detail.review.kind} review</h4>
      <dl className="facts">
        <div><dt>Session</dt><dd><code>{detail.review.session_id}</code></dd></div>
        <div><dt>State</dt><dd>{detail.review.state}</dd></div>
        <div><dt>Version</dt><dd>{detail.review.version}</dd></div>
        {digest && <div><dt>Prepared review digest</dt><dd><code>{digest}</code></dd></div>}
      </dl>
      {detail.presentation ? <pre className="review-presentation">{detail.presentation}</pre> : <p>
        The exact presentation is available through the terminal review workflow.
      </p>}
      {detail.redactions.length > 0 && <p>Presentation omitted: {detail.redactions.join(", ")}</p>}
      {detail.approve_command && detail.can_defer && <div>
        <p>Run in the selected project directory to make the final HUMAN decision.</p>
        <code>{detail.approve_command}</code>
      </div>}
      {!detail.can_defer && <div>
        <p>Run in the selected project directory to continue authoring this decision.</p>
        <code>corinthos {detail.review.kind.toLowerCase()} resume {detail.review.session_id}</code>
      </div>}
      {detail.can_defer && digest && methods.includes("review.defer") && <div className="field-row">
        <label className="checkbox">
          <input type="checkbox" checked={confirmation} disabled={busy}
            onChange={(event) => setConfirmation(event.target.checked)} /> Confirm deferral
        </label>
        <button type="button" disabled={!confirmation || busy} onClick={() => void run(async () => {
          await bridge.reviewDefer(root, { ...detail.review, digest });
          setDetail(null);
          setConfirmation(false);
          setDeferred(true);
          setPage(await bridge.reviews(root, offset));
        }, "The review could not be deferred. Refresh the decision and review its current version.")}>Defer review</button>
      </div>}
    </div>}
  </div>;
}
