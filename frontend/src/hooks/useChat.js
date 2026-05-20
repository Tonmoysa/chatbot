import { useCallback, useRef, useState } from "react";
import { postChat, postDocumentExtract } from "../services/api";
import { getClientIdentity, setSessionId } from "../utils/session";

function extractBotText(payload) {
  const msg = payload?.response?.message;
  return typeof msg === "string" ? msg : "";
}

function friendlyAxiosMessage(err) {
  const data = err?.response?.data;
  if (data && typeof data === "object") {
    const fromEnvelope = extractBotText(data);
    if (fromEnvelope) return fromEnvelope;
    if (typeof data.detail === "string") return data.detail;
  }
  if (err?.code === "ECONNABORTED") {
    return "The request timed out. Please try again.";
  }
  if (!err?.response) {
    return "Unable to reach the server. Check that the API is running and your network connection.";
  }
  return "Something went wrong. Please try again.";
}

export function useChat() {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const sendingRef = useRef(false);

  const clearError = useCallback(() => setError(null), []);

  const sendMessage = useCallback(async (payload) => {
    const text = typeof payload === "string" ? payload : payload?.text || "";
    const file = typeof payload === "object" ? payload?.file : null;
    const trimmed = text.trim();
    if (!trimmed || sendingRef.current) return;
    sendingRef.current = true;

    setError(null);
    setMessages((prev) => [...prev, { role: "user", text: trimmed }]);
    setLoading(true);

    let identity;
    try {
      identity = getClientIdentity();
    } catch (err) {
      setError(err?.message || "Missing CRM identity.");
      setLoading(false);
      sendingRef.current = false;
      return;
    }

    try {
      let documentText = "";
      if (file) {
        const extracted = await postDocumentExtract({ file, identity });
        documentText = extracted.documentText || "";
      }
      const { data, sessionIdHeader } = await postChat({
        message: trimmed,
        sessionId: identity.session_id,
        documentText,
        identity,
      });

      if (sessionIdHeader) {
        setSessionId(sessionIdHeader, identity);
      }

      const topStatus = data?.status;
      const botText = extractBotText(data);
      const display =
        botText ||
        (topStatus === "failed"
          ? "We could not complete that request."
          : "No response message was returned.");

      setMessages((prev) => [...prev, { role: "bot", text: display }]);
    } catch (err) {
      const msg = friendlyAxiosMessage(err);
      setError(msg);
    } finally {
      sendingRef.current = false;
      setLoading(false);
    }
  }, []);

  return { messages, loading, error, sendMessage, clearError };
}
