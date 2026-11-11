import { type FormEvent, useEffect, useRef, useState } from "react";
import { createSession, streamTurn, SessionExpiredError, type SseEvent } from "@/api/chat";

interface ChatMessage {
  id: number;
  role: "assistant" | "user" | "tool";
  content: string;
}

const SUGGESTED_QUESTIONS = [
  "What is driving revenue risk?",
  "Which region needs attention first?",
  "How should I prioritize churn and fraud?",
];

export function FloatingChatbot() {
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 0,
      role: "assistant",
      content: "Ask me about churn risk, fraud patterns, or network quality. I'll investigate using live data.",
    },
  ]);
  const [question, setQuestion] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const sessionIdRef = useRef<string | null>(null);
  const messageIdRef = useRef(1);
  const launcherRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (isOpen) inputRef.current?.focus();
  }, [isOpen]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  useEffect(() => {
    if (!isOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsOpen(false);
        launcherRef.current?.focus();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen]);

  const closeChat = () => {
    setIsOpen(false);
    launcherRef.current?.focus();
  };

  const addMessage = (role: ChatMessage["role"], content: string): number => {
    const id = messageIdRef.current++;
    setMessages((prev) => [...prev, { id, role, content }]);
    return id;
  };

  const appendToMessage = (id: number, chunk: string) => {
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, content: m.content + chunk } : m)),
    );
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmed = question.trim();
    if (!trimmed || isStreaming) return;

    setQuestion("");
    setIsStreaming(true);
    addMessage("user", trimmed);

    try {
      // Create session on first message; auto-recreate if session expired
      if (!sessionIdRef.current) {
        sessionIdRef.current = await createSession();
      }

      const assistantId = addMessage("assistant", "");

      for await (const event of streamTurn(sessionIdRef.current, trimmed)) {
        handleSseEvent(event, assistantId);
      }
    } catch (err) {
      if (err instanceof SessionExpiredError) {
        sessionIdRef.current = null;
        addMessage("assistant", "Session expired — please send your message again.");
      } else {
        addMessage("assistant", `Error: ${err instanceof Error ? err.message : "Unknown error"}`);
      }
    } finally {
      setIsStreaming(false);
    }
  };

  const handleSseEvent = (event: SseEvent, assistantId: number) => {
    if (event.type === "text") {
      appendToMessage(assistantId, event.content);
    } else if (event.type === "tool_call") {
      addMessage("tool", `Called ${event.name}`);
    } else if (event.type === "error") {
      appendToMessage(assistantId, `\n\n⚠️ ${event.content}`);
    }
  };

  const handleSuggestedQuestion = (q: string) => {
    setQuestion(q);
    inputRef.current?.focus();
  };

  return (
    <>
      <button
        ref={launcherRef}
        type="button"
        className="chat-launcher"
        aria-expanded={isOpen}
        aria-controls="dashboard-assistant-panel"
        onClick={() => (isOpen ? closeChat() : setIsOpen(true))}
      >
        <span aria-hidden="true">AI</span>
        {isOpen ? "Close assistant" : "Ask AI"}
      </button>

      {isOpen ? (
        <section
          id="dashboard-assistant-panel"
          className="floating-chatbot"
          role="dialog"
          aria-label="AI assistant"
        >
          <header className="chat-header">
            <div>
              <h2>Revenue assistant</h2>
              <span>{isStreaming ? "Thinking…" : "Ready"}</span>
            </div>
            <button type="button" onClick={closeChat}>
              Close assistant
            </button>
          </header>

          <div className="chat-messages" role="log" aria-live="polite">
            {messages.map((message) => (
              <article className={`chat-message ${message.role}`} key={message.id}>
                {message.content}
              </article>
            ))}
            <div ref={messagesEndRef} />
          </div>

          <div className="chat-suggestions" aria-label="Suggested questions">
            {SUGGESTED_QUESTIONS.map((q) => (
              <button
                type="button"
                key={q}
                onClick={() => handleSuggestedQuestion(q)}
                disabled={isStreaming}
              >
                {q}
              </button>
            ))}
          </div>

          <form className="chat-form" onSubmit={(e) => void handleSubmit(e)}>
            <label htmlFor="dashboard-assistant-question">Question</label>
            <input
              id="dashboard-assistant-question"
              ref={inputRef}
              type="text"
              value={question}
              placeholder="Ask about revenue, churn, fraud, or QoE…"
              disabled={isStreaming}
              onChange={(e) => setQuestion(e.target.value)}
            />
            <button type="submit" disabled={!question.trim() || isStreaming}>
              {isStreaming ? "…" : "Send"}
            </button>
          </form>
        </section>
      ) : null}
    </>
  );
}
