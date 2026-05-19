/** Active STT provider keys (switch via VITE_SPEECH_PROVIDER). */
export const SPEECH_PROVIDERS = {
  WEB_SPEECH: "webSpeech",
  WHISPER: "whisper",
};

const envProvider = import.meta.env.VITE_SPEECH_PROVIDER?.trim();

export const ACTIVE_SPEECH_PROVIDER =
  envProvider === SPEECH_PROVIDERS.WHISPER
    ? SPEECH_PROVIDERS.WHISPER
    : SPEECH_PROVIDERS.WEB_SPEECH;

/** BCP-47 tags tried in order when browser supports them. */
export const SPEECH_LANGUAGE_PREFERENCE = (
  import.meta.env.VITE_SPEECH_LANGUAGES?.trim() || "bn-BD,en-US,en-IN"
)
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

export const SPEECH_CONTINUOUS = true;
export const SPEECH_INTERIM_RESULTS = true;
export const SPEECH_MAX_ALTERNATIVES = 1;

/** Whisper recording limits (Phase 2). */
export const WHISPER_MAX_RECORDING_MS = Number(
  import.meta.env.VITE_WHISPER_MAX_RECORDING_MS || 120_000
);
export const WHISPER_MIME_PREFERENCE = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/ogg;codecs=opus",
];
