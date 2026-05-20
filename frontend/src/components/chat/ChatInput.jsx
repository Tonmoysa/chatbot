import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useSpeechRecognition } from "../../hooks/useSpeechRecognition.js";
import { useVoiceWaveform } from "../../hooks/useVoiceWaveform.js";
import { CheckIcon, CloseIcon, MicIcon } from "./ChatInput.icons.jsx";
import VoiceWaveform from "./VoiceWaveform.jsx";

const MAX_TEXTAREA_HEIGHT = 168;

const QUICK_REPLIES = [
  { label: "Leave Balance", prompt: "Check my leave balance" },
  { label: "Payslip", prompt: "How do I access my payslip?" },
  { label: "Benefits", prompt: "Explain our company benefits" },
  { label: "Contact HR", prompt: "How do I contact HR?" },
];

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

  useEffect(() => {
    const el = textareaRef.current;
    if (!el || isDictating) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }, [value, isDictating]);

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
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) el.style.height = "auto";
    });
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
  const canSend = !disabled && !isDictating && value.trim().length > 0;

  if (isDictating) {
    return (
      <>
        {showError ? (
          <div
            className="chat-footer"
            style={{ borderTop: "none", paddingBottom: 0 }}
            role="alert"
          >
            <div
              style={{
                borderRadius: 8,
                border: "1px solid #fecaca",
                background: "#fef2f2",
                padding: "8px 12px",
                fontSize: "0.85rem",
                color: "#991b1b",
              }}
            >
              {showError}
            </div>
          </div>
        ) : null}
        <div className="dictation-footer" role="region" aria-label="Dictate message">
          <span style={{ color: "var(--hr-danger)", fontWeight: 600 }}>Listening…</span>
          <button type="button" className="action-btn" onClick={() => fileRef.current?.click()} disabled={disabled} title="Attach file" aria-label="Attach file">
            <svg className="hr-icon" viewBox="0 0 24 24">
              <path d="M16.5 6v11.5c0 2.21-1.79 4-4 4s-4-1.79-4-4V5c0-1.38 1.12-2.5 2.5-2.5s2.5 1.12 2.5 2.5v10.5c0 .55-.45 1-1 1s-1-.45-1-1V6H10v9.5c0 1.38 1.12 2.5 2.5 2.5s2.5-1.12 2.5-2.5V5c0-2.21-1.79-4-4-4S7 2.79 7 5v12.5c0 3.04 2.46 5.5 5.5 5.5s5.5-2.46 5.5-5.5V6h-1.5z" />
            </svg>
          </button>
          <input
            ref={fileRef}
            id={fileId}
            type="file"
            accept="image/*,.pdf"
            className="hidden-input"
            onChange={(e) => {
              const f = e.target.files?.[0] || null;
              setFile(f);
            }}
            disabled={disabled}
          />
          <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 4 }}>
            <VoiceWaveform levels={waveformLevels} />
            {sessionText ? (
              <p style={{ fontSize: "0.8rem", margin: 0, color: "var(--hr-text-dark)" }} aria-live="polite">
                {sessionText}
              </p>
            ) : (
              <p style={{ fontSize: "0.75rem", margin: 0, color: "var(--hr-text-light)" }}>Speak in Bangla, Banglish, or English</p>
            )}
          </div>
          <button type="button" className="action-btn" onClick={handleCancel} aria-label="Cancel dictation">
            <CloseIcon className="size-5" />
          </button>
          <button
            type="button"
            className="action-btn send-btn"
            onClick={handleConfirm}
            aria-label="Done — add text to message"
          >
            <CheckIcon className="size-5" />
          </button>
        </div>
      </>
    );
  }

  return (
    <>
      {showError ? (
        <div className="chat-footer" style={{ borderTop: "none", paddingBottom: 0 }} role="alert">
          <div
            style={{
              borderRadius: 8,
              border: "1px solid #fde68a",
              background: "#fffbeb",
              padding: "8px 12px",
              fontSize: "0.85rem",
              color: "#854d0e",
            }}
          >
            {showError}
          </div>
        </div>
      ) : null}

      <form className="chat-footer" onSubmit={(e) => e.preventDefault()}>
        <input
          ref={fileRef}
          id={fileId}
          type="file"
          accept="image/*,.pdf"
          className="hidden-input"
          onChange={(e) => {
            const f = e.target.files?.[0] || null;
            setFile(f);
          }}
          disabled={disabled}
        />

        <div className="quick-replies">
          {QUICK_REPLIES.map((item) => (
            <button
              key={item.label}
              type="button"
              className="quick-btn"
              disabled={disabled}
              onClick={() => {
                onClearError?.();
                onSend(item.prompt);
              }}
            >
              {item.label}
            </button>
          ))}
        </div>

        <div className="input-group">
          <button type="button" className="action-btn" onClick={() => fileRef.current?.click()} title="Attach file" aria-label="Attach file">
            <svg className="hr-icon" viewBox="0 0 24 24">
              <path d="M16.5 6v11.5c0 2.21-1.79 4-4 4s-4-1.79-4-4V5c0-1.38 1.12-2.5 2.5-2.5s2.5 1.12 2.5 2.5v10.5c0 .55-.45 1-1 1s-1-.45-1-1V6H10v9.5c0 1.38 1.12 2.5 2.5 2.5s2.5-1.12 2.5-2.5V5c0-2.21-1.79-4-4-4S7 2.79 7 5v12.5c0 3.04 2.46 5.5 5.5 5.5s5.5-2.46 5.5-5.5V6h-1.5z" />
            </svg>
          </button>

          <div className="input-wrapper">
            {file ? (
              <div className="file-preview-pill">
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{file.name}</span>
                <button
                  type="button"
                  className="remove-file"
                  onClick={() => {
                    setFile(null);
                    if (fileRef.current) fileRef.current.value = "";
                  }}
                  aria-label="Remove file"
                >
                  ✕
                </button>
              </div>
            ) : null}
            <label htmlFor="chat-input-hr" className="sr-only">
              Message
            </label>
            <textarea
              ref={textareaRef}
              id="chat-input-hr"
              className="chat-textarea"
              rows={1}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={onKeyDown}
              disabled={disabled}
              placeholder="Type a message..."
              style={{ maxHeight: MAX_TEXTAREA_HEIGHT }}
            />
          </div>

          {!value.trim() && !file && !disabled ? (
            <button
              type="button"
              className={`action-btn mic-btn ${isDictating ? "recording" : ""}`}
              onClick={handleStartDictation}
              disabled={voiceUnavailable}
              title={voiceUnavailable ? "Voice input is not supported in this browser" : "Dictate message"}
              aria-label="Dictate message"
            >
              <MicIcon className="size-5" />
            </button>
          ) : (
            <button type="button" className="action-btn send-btn" disabled={!canSend} onClick={submit} aria-label="Send message">
              <svg className="hr-icon" viewBox="0 0 24 24" style={{ marginLeft: 2 }}>
                <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" />
              </svg>
            </button>
          )}
        </div>
      </form>
    </>
  );
}
