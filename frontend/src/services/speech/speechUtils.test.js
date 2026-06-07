import { describe, expect, it, vi } from "vitest";
import {
  collapseStutterRepeats,
  mergeFinalTranscriptChunks,
  postProcessBanglaTranscript,
} from "./banglaTranscript.js";
import {
  normalizeTranscript,
  pickBestTranscript,
  refineSpeechSessionFromTranscript,
  resolveSpeechLanguage,
  resolveSpeechSession,
  speechLanguageFallbackChain,
} from "./speechUtils.js";
import { SPEECH_LANG_MODES } from "./speechLanguageDetect.js";

describe("normalizeTranscript", () => {
  it("trims and collapses whitespace", () => {
    expect(normalizeTranscript("  hello   world  ")).toBe("hello world");
  });

  it("preserves Bengali Unicode", () => {
    expect(normalizeTranscript("আমার   ছুটি", { lang: "bn-BD" })).toBe("আমার ছুটি");
  });

  it("handles Banglish mixed text", () => {
    expect(normalizeTranscript("amar  leave balance koto", { lang: "bn-BD" })).toBe(
      "amar leave balance koto"
    );
  });

  it("fixes common roman mis-hearings for banglish mode", () => {
    expect(
      normalizeTranscript("ami corti nite chai", { lang: "en-IN", mode: "banglish" })
    ).toBe("ami chuti nite chai");
  });

  it("skips banglish fixes for english mode", () => {
    expect(
      normalizeTranscript("ami corti nite chai", { lang: "en-US", mode: "en" })
    ).toBe("ami corti nite chai");
  });
});

describe("resolveSpeechLanguage", () => {
  it("forces primary bn-BD when auto-detect is off", () => {
    expect(
      resolveSpeechLanguage(["bn-BD", "en-US"], {
        forcePrimary: true,
        autoDetect: false,
      })
    ).toBe("bn-BD");
  });

  it("can match browser languages when forcePrimary is false", () => {
    vi.stubGlobal("navigator", { languages: ["en-US"], language: "en-US" });
    expect(
      resolveSpeechLanguage(["bn-BD", "en-US"], {
        forcePrimary: false,
        autoDetect: false,
      })
    ).toBe("en-US");
    vi.unstubAllGlobals();
  });
});

describe("resolveSpeechSession", () => {
  it("detects English from draft text when auto-detect is on", () => {
    const s = resolveSpeechSession(["bn-BD", "en-US"], {
      autoDetect: true,
      draftText: "Please check my leave balance for this month",
    });
    expect(s.mode).toBe(SPEECH_LANG_MODES.EN);
    expect(s.lang).toBe("en-US");
  });

  it("detects Banglish from draft text", () => {
    const s = resolveSpeechSession(["bn-BD", "en-IN", "en-US"], {
      autoDetect: true,
      draftText: "amar kalke chuti lagbe sick leave",
    });
    expect(s.mode).toBe(SPEECH_LANG_MODES.BANGLISH);
    expect(s.lang).toBe("en-IN");
  });

  it("defaults to bn-BD when auto-detect has no hints (Bangla-first)", () => {
    vi.stubGlobal("navigator", { languages: ["en-US"], language: "en-US" });
    const s = resolveSpeechSession(["bn-BD", "en-IN", "en-US"], {
      autoDetect: true,
      storedMode: null,
      draftText: "",
    });
    expect(s.mode).toBe(SPEECH_LANG_MODES.BN);
    expect(s.lang).toBe("bn-BD");
    vi.unstubAllGlobals();
  });
});

describe("refineSpeechSessionFromTranscript", () => {
  it("locks Bengali script immediately", () => {
    const r = refineSpeechSessionFromTranscript("আমার আজকে ছুটি লাগবে কারণ শরীর খারাপ", {
      mode: SPEECH_LANG_MODES.EN,
      lang: "en-US",
    });
    expect(r.mode).toBe(SPEECH_LANG_MODES.BN);
    expect(r.lang).toBe("bn-BD");
    expect(r.modeLocked).toBe(true);
  });

  it("does not switch on short interim text", () => {
    const current = { mode: SPEECH_LANG_MODES.BN, lang: "bn-BD" };
    expect(refineSpeechSessionFromTranscript("please check", current)).toEqual(current);
  });

  it("switches to English after enough confident English text", () => {
    const r = refineSpeechSessionFromTranscript(
      "Please check my leave balance for this month and explain the policy",
      { mode: SPEECH_LANG_MODES.BN, lang: "bn-BD" }
    );
    expect(r.mode).toBe(SPEECH_LANG_MODES.EN);
    expect(r.lang).toBe("en-US");
  });
});

describe("pickBestTranscript", () => {
  it("chooses highest confidence alternative", () => {
    const result = [
      { transcript: "wrong", confidence: 0.2 },
      { transcript: "আমার ছুটি", confidence: 0.9 },
    ];
    expect(pickBestTranscript(result)).toBe("আমার ছুটি");
  });
});

describe("mergeFinalTranscriptChunks", () => {
  it("dedupes identical chunks from restart", () => {
    expect(mergeFinalTranscriptChunks("আমার ছুটি", "আমার ছুটি")).toBe("আমার ছুটি");
  });

  it("merges overlapping tail/head words", () => {
    expect(
      mergeFinalTranscriptChunks("ami ajke sick leave", "sick leave nite chai")
    ).toBe("ami ajke sick leave nite chai");
  });
});

describe("postProcessBanglaTranscript", () => {
  it("collapses stutter repeats", () => {
    expect(collapseStutterRepeats("ami ami ajke ajke chuti")).toBe("ami ajke chuti");
  });

  it("builds language fallback chain without duplicates", () => {
    expect(speechLanguageFallbackChain("bn-BD", ["bn-BD", "bn-IN", "en-US"])).toEqual([
      "bn-BD",
      "bn-IN",
      "en-US",
    ]);
  });
});

describe("speechErrorMessage", () => {
  it("maps not-allowed to user-friendly text", async () => {
    const { speechErrorMessage } = await import("./speechUtils.js");
    expect(speechErrorMessage({ error: "not-allowed" })).toMatch(/denied/i);
  });
});
