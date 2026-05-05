import { useCallback, useEffect, useState } from "react";
import ChatBox from "./components/ChatBox";
import { useChat } from "./hooks/useChat";

const THEME_KEY = "hr-chatbot-theme";

function readInitialDark() {
  if (typeof window === "undefined") return false;
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "dark") return true;
    if (stored === "light") return false;
  } catch {
    /* ignore */
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export default function App() {
  const { messages, loading, error, sendMessage, clearError } = useChat();
  const [dark, setDark] = useState(() => readInitialDark());

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
    } catch {
      /* ignore */
    }
  }, [dark]);

  const toggleTheme = useCallback(() => {
    setDark((d) => !d);
  }, []);

  return (
    <div className="flex h-full min-h-0 flex-col bg-slate-50 dark:bg-slate-950">
      <header className="flex shrink-0 items-center justify-between border-b border-slate-200/80 bg-white/90 px-3 py-3 backdrop-blur dark:border-slate-800 dark:bg-slate-950/90 sm:px-5">
        <div className="min-w-0">
          <h1 className="truncate text-base font-semibold tracking-tight text-slate-900 dark:text-white sm:text-lg">
            HR Assistant
          </h1>
          <p className="truncate text-xs text-slate-500 dark:text-slate-400 sm:text-sm">
            Powered by your HR chatbot API
          </p>
        </div>
        <button
          type="button"
          onClick={toggleTheme}
          className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm outline-none transition hover:bg-slate-50 focus-visible:ring-2 focus-visible:ring-emerald-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
          aria-pressed={dark}
          aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
        >
          {dark ? "Light" : "Dark"}
        </button>
      </header>

      <main className="flex min-h-0 flex-1 flex-col">
        <ChatBox
          messages={messages}
          loading={loading}
          error={error}
          onSend={sendMessage}
          onClearError={clearError}
        />
      </main>
    </div>
  );
}
