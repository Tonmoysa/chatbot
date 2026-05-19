import { SPEECH_LANGUAGE_PREFERENCE } from "./speechConfig.js";

/**
 * @returns {typeof SpeechRecognition | null}
 */
export function getSpeechRecognitionConstructor() {
  if (typeof window === "undefined") return null;
  const w = window;
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

export function isWebSpeechSupported() {
  return Boolean(getSpeechRecognitionConstructor());
}

export function isMediaRecorderSupported() {
  return typeof window !== "undefined" && typeof MediaRecorder !== "undefined";
}

/**
 * Pick the first BCP-47 tag the browser reports as available.
 * @param {string[]} [preference]
 */
export function resolveSpeechLanguage(preference = SPEECH_LANGUAGE_PREFERENCE) {
  if (typeof navigator === "undefined") {
    return preference[0] || "en-US";
  }
  const available = new Set(
    (navigator.languages || [navigator.language || "en-US"]).map((l) =>
      l.toLowerCase()
    )
  );
  for (const tag of preference) {
    const lower = tag.toLowerCase();
    if (available.has(lower)) return tag;
    const base = lower.split("-")[0];
    if ([...available].some((a) => a === base || a.startsWith(`${base}-`))) {
      return tag;
    }
  }
  return preference[0] || "en-US";
}

/**
 * Normalize transcript for chat input (Unicode-safe, Banglish-friendly).
 * @param {string} text
 */
export function normalizeTranscript(text) {
  if (!text || typeof text !== "string") return "";
  let t = text.normalize("NFC");
  t = t.replace(/\u00A0/g, " ");
  t = t.replace(/[\u200B-\u200D\uFEFF]/g, "");
  t = t.replace(/\s+/g, " ");
  t = t.replace(/\s+([,.!?;:])/g, "$1");
  t = t.replace(/([,.!?;:])([^\s])/g, "$1 $2");
  return t.trim();
}

/**
 * @param {unknown} err
 */
export function speechErrorMessage(err) {
  if (!err) return "Speech recognition failed.";
  if (typeof err === "string") return err;
  const code = err.error || err.name || "";
  const messages = {
    "not-allowed": "Microphone access was denied. Allow the microphone in your browser settings and try again.",
    "service-not-allowed": "Speech recognition is blocked on this page. Use HTTPS or check browser permissions.",
    "no-speech": "No speech was detected. Try speaking again.",
    "audio-capture": "No microphone was found. Connect a microphone and try again.",
    "network": "Network error during speech recognition. Check your connection.",
    "aborted": "Speech recognition was stopped.",
    "language-not-supported": "This language is not supported for speech recognition in your browser.",
  };
  if (code && messages[code]) return messages[code];
  if (err.message && typeof err.message === "string") return err.message;
  return "Speech recognition failed. Please try again or type your message.";
}

export function canUseSpeechProvider(providerKey) {
  if (providerKey === "webSpeech") return isWebSpeechSupported();
  if (providerKey === "whisper") return isMediaRecorderSupported();
  return false;
}
