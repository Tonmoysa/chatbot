import axios from "axios";

const resolvedBaseURL = () => {
  const fromEnv = import.meta.env.VITE_API_BASE_URL?.trim();
  if (fromEnv) return fromEnv.replace(/\/$/, "");
  if (import.meta.env.DEV) return "";
  return "http://127.0.0.1:8000";
};

const client = axios.create({
  baseURL: resolvedBaseURL(),
  headers: { "Content-Type": "application/json" },
  timeout: 120_000,
});

client.interceptors.request.use((config) => {
  const key = import.meta.env.VITE_HR_API_KEY?.trim();
  if (key) {
    config.headers["X-API-Key"] = key;
  }
  return config;
});

/**
 * POST /api/chat/
 * @returns {{ data: object, sessionIdHeader: string | null }}
 */
export async function postChat({ message, sessionId }) {
  const res = await client.post("/api/chat/", {
    message,
    session_id: sessionId,
  });
  const sessionIdHeader =
    res.headers["x-session-id"] ?? res.headers["X-Session-Id"] ?? null;
  return {
    data: res.data,
    sessionIdHeader: typeof sessionIdHeader === "string" ? sessionIdHeader : null,
  };
}
