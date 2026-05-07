import { useCallback, useId, useRef, useState } from "react";

export default function InputBox({ onSend, disabled, onClearError, error }) {
  const [value, setValue] = useState("");
  const [file, setFile] = useState(null);
  const fileId = useId();
  const fileRef = useRef(null);

  const submit = useCallback(() => {
    if (disabled) return;
    const t = value.trim();
    if (!t) return;
    onClearError?.();
    onSend(file ? { text: t, file } : t);
    setValue("");
    setFile(null);
    if (fileRef.current) fileRef.current.value = "";
  }, [disabled, onSend, onClearError, value, file]);

  const onKeyDown = useCallback(
    (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submit();
      }
    },
    [submit]
  );

  return (
    <div className="border-t border-slate-200/90 bg-slate-50/95 px-3 py-3 backdrop-blur dark:border-slate-800 dark:bg-slate-950/95 sm:px-4">
      {error ? (
        <div
          className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-100"
          role="alert"
        >
          {error}
        </div>
      ) : null}
      <div className="mx-auto flex max-w-3xl flex-col gap-2">
        <div className="flex items-end gap-2">
          <input
            ref={fileRef}
            id={fileId}
            type="file"
            accept="image/*,.pdf"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0] || null;
              setFile(f);
            }}
            disabled={disabled}
          />
          <button
            type="button"
            onClick={() => document.getElementById(fileId)?.click()}
            disabled={disabled}
            className="inline-flex h-[44px] shrink-0 items-center justify-center rounded-xl border border-slate-200 bg-white px-3 text-sm font-semibold text-slate-700 shadow-sm outline-none transition hover:bg-slate-50 focus-visible:ring-2 focus-visible:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
            aria-label="Attach receipt"
          >
            Attach
          </button>
        <label htmlFor="chat-input" className="sr-only">
          Message
        </label>
        <textarea
          id="chat-input"
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          placeholder="Ask about leave, policies, or HR requests…"
          className="min-h-[44px] flex-1 resize-none rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-[15px] text-slate-900 shadow-sm outline-none ring-emerald-500/0 transition focus:border-emerald-500 focus:ring-2 focus:ring-emerald-500/30 disabled:cursor-not-allowed disabled:opacity-60 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:focus:border-emerald-500"
        />
        <button
          type="button"
          onClick={submit}
          disabled={disabled || !value.trim()}
          className="inline-flex shrink-0 items-center justify-center rounded-xl bg-emerald-600 px-4 py-2 text-sm font-semibold text-white shadow-sm outline-none transition hover:bg-emerald-700 focus-visible:ring-2 focus-visible:ring-emerald-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-emerald-600 dark:hover:bg-emerald-500 dark:focus-visible:ring-offset-slate-950"
          aria-label="Send message"
        >
          Send
        </button>
        </div>

        {file ? (
          <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs text-slate-700 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200">
            <div className="min-w-0">
              <span className="font-medium">Receipt:</span>{" "}
              <span className="truncate">{file.name}</span>
            </div>
            <button
              type="button"
              onClick={() => {
                setFile(null);
                if (fileRef.current) fileRef.current.value = "";
              }}
              disabled={disabled}
              className="ml-3 rounded-lg px-2 py-1 text-xs font-semibold text-slate-600 hover:bg-slate-50 disabled:opacity-50 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              Remove
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
