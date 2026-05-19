import { useCallback, useId, useRef, useState } from "react";
import { useSpeechRecognition } from "../../hooks/useSpeechRecognition.js";
import { useVoiceWaveform } from "../../hooks/useVoiceWaveform.js";
import VoiceWaveform from "./VoiceWaveform.jsx";

function MicIcon({ className }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
      <path d="M19 10v1a7 7 0 0 1-14 0v-1" />
      <path d="M12 18v4" />
      <path d="M8 22h8" />
    </svg>
  );
}

function CheckIcon({ className }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

function CloseIcon({ className }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );
}

export default function ChatInput({ onSend, disabled, onClearError, error }) {
  const [value, setValue] = useState("");
  const [file, setFile] = useState(null);
  const [localError, setLocalError] = useState(null);
  const fileId = useId();
  const fileRef = useRef(null);
  const textareaRef = useRef(null);

  const {
    isDictating,
    sessionText,
    speechError,
    capabilities,
    startDictation,
    cancelDictation,
    confirmDictation,
    clearSpeechError,
  } = useSpeechRecognition({ disabled });

  const waveformLevels = useVoiceWaveform(isDictating);

  const handleStartDictation = useCallback(async () => {
    setLocalError(null);
    onClearError?.();
    clearSpeechError();
    await startDictation();
  }, [startDictation, clearSpeechError, onClearError]);

  const handleConfirm = useCallback(async () => {
    setLocalError(null);
    const transcript = await confirmDictation();
    if (!transcript) {
      setLocalError("No speech detected. Speak clearly, then tap the checkmark.");
      return;
    }
    setValue((prev) => {
      const trimmed = prev.trim();
      return trimmed ? `${trimmed} ${transcript}` : transcript;
    });
    clearSpeechError();
    onClearError?.();
    requestAnimationFrame(() => textareaRef.current?.focus());
  }, [confirmDictation, clearSpeechError, onClearError]);

  const handleCancel = useCallback(() => {
    setLocalError(null);
    cancelDictation();
  }, [cancelDictation]);

  const submit = useCallback(() => {
    if (disabled || isDictating) return;
    const t = value.trim();
    if (!t) return;
    onClearError?.();
    clearSpeechError();
    setLocalError(null);
    onSend(file ? { text: t, file } : t);
    setValue("");
    setFile(null);
    if (fileRef.current) fileRef.current.value = "";
  }, [disabled, isDictating, onSend, onClearError, value, file, clearSpeechError]);

  const onKeyDown = useCallback(
    (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submit();
      }
    },
    [submit]
  );

  const showError = error || speechError || localError;
  const voiceUnavailable = !capabilities.supported;

  return (
    <div className="border-t border-slate-200/90 bg-slate-50/95 px-3 py-3 backdrop-blur dark:border-slate-800 dark:bg-slate-950/95 sm:px-4">
      {showError ? (
        <div
          className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-950 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-100"
          role="alert"
        >
          {showError}
        </div>
      ) : null}

      <div className="mx-auto flex max-w-3xl flex-col gap-2">
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
          disabled={disabled || isDictating}
        />

        {isDictating ? (
          <div
            className="flex items-center gap-2 rounded-[28px] border border-slate-300/80 bg-white px-3 py-2.5 shadow-md dark:border-slate-600 dark:bg-slate-900"
            role="region"
            aria-label="Dictate message"
          >
            <button
              type="button"
              onClick={() => document.getElementById(fileId)?.click()}
              disabled={disabled}
              className="inline-flex size-9 shrink-0 items-center justify-center rounded-full text-slate-500 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
              aria-label="Attach file"
            >
              <span className="text-xl font-light leading-none">+</span>
            </button>

            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <div className="flex min-h-9 items-center gap-2">
                <div
                  className="hidden flex-1 border-b border-dotted border-slate-300 dark:border-slate-600 sm:block"
                  aria-hidden
                />
                <VoiceWaveform levels={waveformLevels} />
              </div>
              {sessionText ? (
                <p
                  className="truncate px-1 text-sm text-slate-700 dark:text-slate-200"
                  aria-live="polite"
                >
                  {sessionText}
                </p>
              ) : (
                <p className="px-1 text-xs text-slate-500 dark:text-slate-400">
                  Listening… speak in Bangla, Banglish, or English
                </p>
              )}
            </div>

            <button
              type="button"
              onClick={handleCancel}
              className="inline-flex size-9 shrink-0 items-center justify-center rounded-full text-slate-500 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
              aria-label="Cancel dictation"
            >
              <CloseIcon className="size-5" />
            </button>

            <button
              type="button"
              onClick={handleConfirm}
              className="inline-flex size-9 shrink-0 items-center justify-center rounded-full bg-emerald-600 text-white shadow-sm hover:bg-emerald-500 focus-visible:ring-2 focus-visible:ring-emerald-500"
              aria-label="Done — add text to message"
              title="Done"
            >
              <CheckIcon className="size-5" />
            </button>
          </div>
        ) : (
          <div className="flex items-end gap-2">
            <button
              type="button"
              onClick={() => document.getElementById(fileId)?.click()}
              disabled={disabled}
              className="inline-flex h-[44px] shrink-0 items-center justify-center rounded-xl border border-slate-200 bg-white px-3 text-sm font-semibold text-slate-700 shadow-sm outline-none transition hover:bg-slate-50 focus-visible:ring-2 focus-visible:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
              aria-label="Attach receipt"
            >
              Attach
            </button>

            <button
              type="button"
              onClick={handleStartDictation}
              disabled={disabled || voiceUnavailable}
              title={
                voiceUnavailable
                  ? "Voice input is not supported in this browser"
                  : "Dictate message"
              }
              className="inline-flex h-[44px] shrink-0 items-center justify-center rounded-xl border border-slate-200 bg-white px-3 text-slate-700 shadow-sm outline-none transition hover:bg-slate-50 focus-visible:ring-2 focus-visible:ring-emerald-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
              aria-label="Dictate message"
            >
              <MicIcon className="size-5" />
            </button>

            <label htmlFor="chat-input" className="sr-only">
              Message
            </label>
            <textarea
              ref={textareaRef}
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
        )}

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
