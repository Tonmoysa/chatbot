import { useEffect, useRef } from "react";
import MessageBubble from "./MessageBubble";
import InputBox from "./InputBox";

export default function ChatBox({ messages, loading, error, onSend, onClearError }) {
  const scrollRef = useRef(null);
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, loading]);

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col bg-slate-100/80 dark:bg-slate-900/80">
      <div
        ref={scrollRef}
        className="min-h-0 flex-1 overflow-y-auto px-3 py-4 sm:px-6"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
      >
        <div className="mx-auto flex max-w-3xl flex-col gap-3">
          {messages.length === 0 && !loading ? (
            <div className="rounded-2xl border border-dashed border-slate-300/80 bg-white/60 px-4 py-8 text-center text-sm text-slate-600 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-400">
              <p className="font-medium text-slate-800 dark:text-slate-200">HR Assistant</p>
              <p className="mt-2 max-w-md mx-auto">
                Ask about PTO, leave requests, policies, or request status. Messages are sent to your
                secure HR service.
              </p>
            </div>
          ) : null}

          {messages.map((m, i) => (
            <MessageBubble key={`${i}-${m.role}-${m.text.slice(0, 24)}`} role={m.role} text={m.text} />
          ))}

          {loading ? (
            <div className="flex justify-start" aria-live="assertive">
              <div className="rounded-2xl bg-white px-4 py-2.5 text-sm text-slate-500 shadow-sm ring-1 ring-slate-200/80 dark:bg-slate-900 dark:text-slate-400 dark:ring-slate-700">
                <span className="inline-flex items-center gap-2">
                  <span
                    className="inline-block size-2 animate-pulse rounded-full bg-emerald-500"
                    aria-hidden
                  />
                  Typing…
                </span>
              </div>
            </div>
          ) : null}

          <div ref={bottomRef} className="h-px w-full shrink-0" aria-hidden />
        </div>
      </div>

      <InputBox onSend={onSend} disabled={loading} error={error} onClearError={onClearError} />
    </div>
  );
}
