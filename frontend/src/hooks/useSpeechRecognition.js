import { useCallback, useEffect, useRef, useState } from "react";
import {
  abortRecognition,
  getSpeechCapabilities,
  setSpeechCallbacks,
  startRecognition,
  stopRecognition,
} from "../services/speech/speechService.js";
import { normalizeTranscript } from "../services/speech/speechUtils.js";
import { generateTraceId, logSpeechEvent } from "../utils/trace.js";

/**
 * Dictate-mode speech hook: accumulates transcript until user confirms (✓).
 */
export function useSpeechRecognition({ disabled = false } = {}) {
  const [isDictating, setIsDictating] = useState(false);
  const [sessionText, setSessionText] = useState("");
  const [interimText, setInterimText] = useState("");
  const [speechError, setSpeechError] = useState(null);

  const traceIdRef = useRef(null);
  const finalPartsRef = useRef([]);
  const interimRef = useRef("");
  const capabilities = getSpeechCapabilities();

  const rebuildSessionText = useCallback((interim = "") => {
    const finals = finalPartsRef.current.join(" ").trim();
    const combined = [finals, interim.trim()].filter(Boolean).join(" ");
    setSessionText(normalizeTranscript(combined));
  }, []);

  useEffect(() => {
    setSpeechCallbacks({
      onStart: () => {
        setSpeechError(null);
        setInterimText("");
      },
      onResult: (text, isFinal) => {
        const normalized = normalizeTranscript(text);
        if (!normalized) return;

        if (isFinal) {
          finalPartsRef.current.push(normalized);
          interimRef.current = "";
          setInterimText("");
          rebuildSessionText("");
          logSpeechEvent("speech_session_final_chunk", {
            traceId: traceIdRef.current,
            chars: normalized.length,
          });
        } else {
          interimRef.current = normalized;
          setInterimText(normalized);
          rebuildSessionText(normalized);
        }
      },
      onError: (message) => {
        setSpeechError(message);
        setIsDictating(false);
        setInterimText("");
        logSpeechEvent("speech_hook_error", {
          message,
          traceId: traceIdRef.current,
        });
      },
      onEnd: () => {
        setInterimText("");
        rebuildSessionText("");
      },
    });

    return () => {
      abortRecognition().catch(() => {});
    };
  }, [rebuildSessionText]);

  const resetSession = useCallback(() => {
    finalPartsRef.current = [];
    interimRef.current = "";
    setSessionText("");
    setInterimText("");
  }, []);

  const startDictation = useCallback(async () => {
    if (disabled) return;
    if (!capabilities.supported) {
      setSpeechError(
        "Voice input is not available in this browser. Try Chrome or Edge, or type your message."
      );
      return;
    }

    resetSession();
    setSpeechError(null);
    setIsDictating(true);
    traceIdRef.current = generateTraceId();

    logSpeechEvent("speech_user_start", {
      provider: capabilities.provider,
      traceId: traceIdRef.current,
      webSpeech: capabilities.webSpeech,
      whisper: capabilities.whisper,
    });

    try {
      await startRecognition({ traceId: traceIdRef.current });
    } catch (err) {
      setSpeechError(err?.message || "Could not start voice input.");
      setIsDictating(false);
    }
  }, [disabled, capabilities, resetSession]);

  const cancelDictation = useCallback(async () => {
    try {
      await abortRecognition();
    } catch {
      /* ignore */
    }
    resetSession();
    setIsDictating(false);
    logSpeechEvent("speech_dictate_cancel", { traceId: traceIdRef.current });
  }, [resetSession]);

  const confirmDictation = useCallback(async () => {
    try {
      await stopRecognition();
    } catch {
      /* ignore */
    }

    await new Promise((r) => setTimeout(r, 200));

    const transcript = normalizeTranscript(
      [finalPartsRef.current.join(" "), interimRef.current].filter(Boolean).join(" ")
    );

    logSpeechEvent("speech_dictate_confirm", {
      traceId: traceIdRef.current,
      chars: transcript.length,
    });

    resetSession();
    setIsDictating(false);
    return transcript;
  }, [resetSession]);

  const clearSpeechError = useCallback(() => setSpeechError(null), []);

  const displayText =
    sessionText || interimText || (isDictating ? "" : "");

  return {
    isDictating,
    sessionText: displayText,
    interimText,
    speechError,
    capabilities,
    startDictation,
    cancelDictation,
    confirmDictation,
    clearSpeechError,
  };
}
