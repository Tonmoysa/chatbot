const STORAGE_KEY = "hr-chatbot-session-id";

function newSessionId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `session-${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
}

export function getSessionId() {
  try {
    const existing = localStorage.getItem(STORAGE_KEY);
    if (existing && existing.trim()) {
      return existing.trim();
    }
  } catch {
    /* ignore */
  }
  const id = newSessionId();
  setSessionId(id);
  return id;
}

export function setSessionId(sessionId) {
  const v = (sessionId || "").trim();
  if (!v) return;
  try {
    localStorage.setItem(STORAGE_KEY, v);
  } catch {
    /* ignore */
  }
}
