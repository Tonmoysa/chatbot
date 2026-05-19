import {
  SPEECH_CONTINUOUS,
  SPEECH_INTERIM_RESULTS,
  SPEECH_MAX_ALTERNATIVES,
} from "../speechConfig.js";
import {
  getSpeechRecognitionConstructor,
  normalizeTranscript,
  resolveSpeechLanguage,
  speechErrorMessage,
} from "../speechUtils.js";
import { logSpeechEvent } from "../../../utils/trace.js";
import { BaseSpeechProvider } from "./baseProvider.js";

export class WebSpeechProvider extends BaseSpeechProvider {
  name = "webSpeech";

  /** @type {SpeechRecognition | null} */
  #recognition = null;
  #listening = false;
  #shouldRestart = false;
  #language = "en-US";
  #traceId = "";

  isSupported() {
    return Boolean(getSpeechRecognitionConstructor());
  }

  /**
   * @param {{ language?: string, traceId?: string }} [options]
   */
  async start(options = {}) {
    const Ctor = getSpeechRecognitionConstructor();
    if (!Ctor) {
      throw new Error(
        "Speech recognition is not supported in this browser. Try Chrome or Edge on desktop."
      );
    }

    this.#language = options.language || resolveSpeechLanguage();
    this.#traceId = options.traceId || "";
    this.#shouldRestart = true;

    if (this.#recognition) {
      try {
        this.#recognition.abort();
      } catch {
        /* ignore */
      }
    }

    const recognition = new Ctor();
    recognition.lang = this.#language;
    recognition.continuous = SPEECH_CONTINUOUS;
    recognition.interimResults = SPEECH_INTERIM_RESULTS;
    recognition.maxAlternatives = SPEECH_MAX_ALTERNATIVES;

    recognition.onstart = () => {
      this.#listening = true;
      logSpeechEvent("speech_recognition_start", {
        provider: this.name,
        traceId: this.#traceId,
        language: this.#language,
      });
      this.emitStart();
    };

    recognition.onresult = (event) => {
      let interim = "";
      let final = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        const transcript = result[0]?.transcript || "";
        if (result.isFinal) {
          final += transcript;
        } else {
          interim += transcript;
        }
      }
      const raw = final || interim;
      const text = normalizeTranscript(raw);
      if (!text) return;
      const isFinal = Boolean(final);
      if (isFinal) {
        logSpeechEvent("speech_transcript_final", {
          provider: this.name,
          traceId: this.#traceId,
          chars: text.length,
        });
      }
      this.emitResult(text, isFinal);
    };

    recognition.onerror = (event) => {
      const msg = speechErrorMessage(event);
      logSpeechEvent("speech_recognition_error", {
        provider: this.name,
        traceId: this.#traceId,
        code: event.error,
      });
      if (event.error === "aborted") return;
      this.#shouldRestart = false;
      this.emitError(msg);
    };

    recognition.onend = () => {
      this.#listening = false;
      logSpeechEvent("speech_recognition_stop", {
        provider: this.name,
        traceId: this.#traceId,
      });
      if (this.#shouldRestart && this.#recognition === recognition) {
        try {
          recognition.start();
          return;
        } catch {
          this.#shouldRestart = false;
        }
      }
      this.emitEnd();
    };

    this.#recognition = recognition;
    recognition.start();
  }

  async stop() {
    this.#shouldRestart = false;
    const recognition = this.#recognition;
    if (!recognition) return;
    try {
      recognition.stop();
    } catch {
      try {
        recognition.abort();
      } catch {
        /* ignore */
      }
    }
    this.#recognition = null;
    this.#listening = false;
  }

  async abort() {
    this.#shouldRestart = false;
    const recognition = this.#recognition;
    if (!recognition) return;
    try {
      recognition.abort();
    } catch {
      /* ignore */
    }
    this.#recognition = null;
    this.#listening = false;
    this.emitEnd();
  }

  get listening() {
    return this.#listening;
  }
}
