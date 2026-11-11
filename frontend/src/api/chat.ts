export type SseEvent =
  | { type: "text"; content: string }
  | { type: "tool_call"; name: string; arguments: Record<string, unknown>; result: unknown }
  | { type: "error"; content: string };

export async function createSession(): Promise<string> {
  const res = await fetch("/api/chat/sessions", { method: "POST" });
  if (!res.ok) throw new Error(`Session creation failed: ${res.status}`);
  const data = await res.json();
  return data.session_id as string;
}

export async function* streamTurn(
  sessionId: string,
  message: string,
): AsyncGenerator<SseEvent> {
  const res = await fetch(`/api/chat/sessions/${sessionId}/turns`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });

  if (!res.ok) {
    if (res.status === 404) {
      throw new SessionExpiredError();
    }
    throw new Error(`Turn request failed: ${res.status}`);
  }

  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = line.slice(6).trim();
        if (payload === "[DONE]") return;
        try {
          yield JSON.parse(payload) as SseEvent;
        } catch {
          // skip malformed chunks
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export class SessionExpiredError extends Error {
  constructor() {
    super("Session not found — backend may have restarted");
  }
}
