import { useRef, useState, type FormEvent, type ReactElement } from 'react';
import * as React from 'react';
import { z } from 'zod';
import { ApiError, sendChatMessage, type ChatMessageRequest } from '../services/api';

type ChatEntry =
  | { id: string; role: 'user'; status: 'complete'; text: string }
  | { id: string; role: 'assistant'; status: 'complete'; text: string }
  | { id: string; role: 'assistant'; status: 'pending'; text: string }
  | { id: string; role: 'assistant'; status: 'error'; text: string; code: string };

const sessionIdSchema = z.string().uuid();
const sessionStorageKey = 'telco-revenue-assurance-chat-session';

function getSessionId(): string {
  try {
    const existingSessionId = window.sessionStorage.getItem(sessionStorageKey);
    if (existingSessionId && sessionIdSchema.safeParse(existingSessionId).success) {
      return existingSessionId;
    }

    const sessionId = crypto.randomUUID();
    window.sessionStorage.setItem(sessionStorageKey, sessionId);
    return sessionId;
  } catch {
    return crypto.randomUUID();
  }
}

function errorDetails(error: unknown): { code: string; message: string } {
  if (error instanceof ApiError) {
    return { code: error.code, message: error.message };
  }
  return {
    code: 'REQUEST_FAILED',
    message: 'The reply could not be completed. Your question is still available above.',
  };
}

export function ChatPanel(): ReactElement {
  const [sessionId] = useState<string>(getSessionId);
  const [messages, setMessages] = useState<ChatEntry[]>([]);
  const [draft, setDraft] = useState('');
  const [isPending, setIsPending] = useState(false);
  const pendingRef = useRef(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const message = draft.trim();
    if (!message || pendingRef.current) {
      return;
    }

    const request = {
      request_id: crypto.randomUUID(),
      message,
    } satisfies ChatMessageRequest;
    const assistantEntryId = `assistant-${request.request_id}`;
    pendingRef.current = true;
    setIsPending(true);
    setDraft('');
    setMessages((current) => [
      ...current,
      { id: `user-${request.request_id}`, role: 'user', status: 'complete', text: message },
      {
        id: assistantEntryId,
        role: 'assistant',
        status: 'pending',
        text: 'Working through your question…',
      },
    ]);

    try {
      const response = await sendChatMessage(sessionId, request);
      setMessages((current) =>
        current.map((entry) =>
          entry.id === assistantEntryId
            ? {
                id: assistantEntryId,
                role: 'assistant',
                status: 'complete',
                text: response.assistant_message,
              }
            : entry,
        ),
      );
    } catch (error: unknown) {
      const details = errorDetails(error);
      setMessages((current) =>
        current.map((entry) =>
          entry.id === assistantEntryId
            ? {
                id: assistantEntryId,
                role: 'assistant',
                status: 'error',
                text: details.message,
                code: details.code,
              }
            : entry,
        ),
      );
    } finally {
      pendingRef.current = false;
      setIsPending(false);
    }
  }

  return (
    <React.Fragment>
      <section
        className="ra-card ra-chat-card"
        aria-labelledby="chat-heading"
        aria-busy={isPending}
      >
        <div className="ra-card-heading ra-chat-heading">
          <div>
            <p className="ra-eyebrow">Analyst workspace</p>
            <h2 id="chat-heading">Ask the assurance agent</h2>
          </div>
          <span className="ra-chat-mark" aria-hidden="true">
            ✦
          </span>
        </div>
        <p className="ra-chat-description">
          Ask about churn, transaction fraud, or service quality. Replies arrive as a single
          response.
        </p>
        <div className="ra-chat-transcript" aria-live="polite" aria-relevant="additions text">
          {messages.length === 0 ? (
            <div className="ra-chat-empty">
              <span className="ra-chat-empty-icon" aria-hidden="true">
                ↗
              </span>
              <p>Start with a question about the selected scoring run.</p>
              <span>For example: “Which class has the greatest customer dollar weight?”</span>
            </div>
          ) : (
            <ol className="ra-chat-messages">
              {messages.map((entry) => (
                <li className={`ra-message ra-message-${entry.role}`} key={entry.id}>
                  <span className="ra-message-label">
                    {entry.role === 'user' ? 'You' : 'Assurance agent'}
                  </span>
                  {entry.status === 'pending' ? (
                    <div
                      className="ra-message-pending"
                      role="status"
                      aria-label="Assistant response pending"
                    >
                      <span className="ra-pulse" aria-hidden="true" />
                      {entry.text}
                    </div>
                  ) : entry.status === 'error' ? (
                    <div className="ra-message-error" role="alert">
                      <strong>{entry.code}</strong>
                      <span>{entry.text}</span>
                    </div>
                  ) : (
                    <p className="ra-message-text">{entry.text}</p>
                  )}
                </li>
              ))}
            </ol>
          )}
        </div>
        <form className="ra-chat-form" onSubmit={handleSubmit}>
          <label className="ra-visually-hidden" htmlFor="chat-message">
            Ask the revenue assurance agent
          </label>
          <textarea
            id="chat-message"
            maxLength={4000}
            onChange={(event) => setDraft(event.currentTarget.value)}
            placeholder="Ask about a scoring run…"
            rows={3}
            value={draft}
          />
          <div className="ra-chat-form-footer">
            <span>Responses may take a moment.</span>
            <button
              className="ra-button ra-button-send"
              disabled={!draft.trim() || isPending}
              type="submit"
            >
              {isPending ? 'Sending…' : 'Send question'}
              <span aria-hidden="true">↗</span>
            </button>
          </div>
        </form>
      </section>
    </React.Fragment>
  );
}
