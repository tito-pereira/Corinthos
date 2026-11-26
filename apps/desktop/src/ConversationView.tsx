// SPDX-License-Identifier: MPL-2.0
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import styles from "./ConversationView.module.css";

export interface ConversationMessage {
  role: "user" | "assistant";
  text: string;
  status?: string;
  error?: string;
  operation_id?: string;
  run_id?: string;
  truncated?: boolean;
}

export interface ConversationViewProps {
  title: string;
  messages: ConversationMessage[];
  busy: boolean;
  error?: string;
  onSend: (message: string) => void;
  onCancel?: () => void;
  toolbar?: ReactNode;
  actions?: ReactNode;
}

export function ConversationView({
  title, messages, busy, error, onSend, onCancel, toolbar, actions,
}: ConversationViewProps) {
  const [draft, setDraft] = useState("");
  const helpId = useId();
  const end = useRef<HTMLDivElement>(null);
  const empty = messages.length === 0;

  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [messages, busy]);

  function send() {
    const message = draft.trim();
    if (!message || busy) return;
    onSend(message);
    setDraft("");
  }

  return <section className={`${styles.conversation} ${empty ? styles.empty : ""}`} aria-label="Conversation">
    <div className={empty ? styles.welcome : styles.body}>
      <h1 className={empty ? styles.title : styles.visuallyHidden}>{title}</h1>
      {!empty && <div className={styles.messages} role="log" aria-label="Conversation messages" aria-live="polite" aria-busy={busy}>
        {messages.map((message, index) => <article
          key={index}
          className={`${styles.message} ${message.role === "user" ? styles.user : styles.assistant}`}
          aria-label={`${message.role === "user" ? "You" : "Corinthos"} message`}
        >
          <div className={styles.messageHeading}>
            <strong>{message.role === "user" ? "You" : "Corinthos"}</strong>
            {message.status && <span className={styles.status}>{message.status}</span>}
          </div>
          <p className={styles.messageText}>{message.text}</p>
          {message.truncated && <p className={styles.notice}>Earlier output omitted from this view.</p>}
          {message.error && <p className={styles.messageError} role="alert">{message.error}</p>}
        </article>)}
        <div ref={end} />
      </div>}
    </div>
    <div className={styles.dock}>
      {error && <p className={styles.error} role="alert">{error}</p>}
      {actions && <div className={styles.actions} aria-label="Run actions">{actions}</div>}
      {toolbar && <div className={styles.toolbar}>{toolbar}</div>}
      <form className={styles.composer} aria-label="Send a message" onSubmit={event => { event.preventDefault(); send(); }}>
        <label className={styles.visuallyHidden} htmlFor={`${helpId}-message`}>Message</label>
        <textarea
          id={`${helpId}-message`}
          aria-describedby={helpId}
          placeholder="Ask Corinthos…"
          value={draft}
          maxLength={4096}
          rows={2}
          disabled={busy}
          onChange={event => setDraft(event.target.value)}
          onKeyDown={event => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              if (!event.repeat) send();
            }
          }}
        />
        <div className={styles.buttons}>
          {busy && onCancel && <button type="button" className={styles.cancel} onClick={onCancel}>Cancel</button>}
          <button type="submit" className={styles.send} disabled={busy || !draft.trim()}>Send</button>
        </div>
      </form>
      <div className={styles.composerNote}>
        <span id={helpId}>Enter to send · Shift+Enter for a new line</span>
        {busy && <span role="status">Corinthos is working…</span>}
      </div>
    </div>
  </section>;
}
