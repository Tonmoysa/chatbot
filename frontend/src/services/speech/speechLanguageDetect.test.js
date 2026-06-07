import { describe, expect, it } from "vitest";
import {
  SPEECH_LANG_MODES,
  detectSpeechLanguageMode,
  scoreTranscriptForMode,
  speechLangChainForMode,
  speechLangTagForMode,
} from "./speechLanguageDetect.js";

describe("detectSpeechLanguageMode", () => {
  it("detects Bengali script", () => {
    const r = detectSpeechLanguageMode("আমার আজকে ছুটি লাগবে কারণ শরীর খারাপ");
    expect(r.mode).toBe(SPEECH_LANG_MODES.BN);
    expect(r.confidence).toBeGreaterThan(0.5);
  });

  it("detects English", () => {
    const r = detectSpeechLanguageMode("Please check my leave balance for this month");
    expect(r.mode).toBe(SPEECH_LANG_MODES.EN);
    expect(r.confidence).toBeGreaterThan(0.4);
  });

  it("detects Banglish", () => {
    const r = detectSpeechLanguageMode("amar soril kharap tai kalke full day paid leave lagbe");
    expect(r.mode).toBe(SPEECH_LANG_MODES.BANGLISH);
    expect(r.confidence).toBeGreaterThan(0.4);
  });

  it("returns unknown for very short text", () => {
    expect(detectSpeechLanguageMode("hi").mode).toBe(SPEECH_LANG_MODES.UNKNOWN);
  });
});

describe("speechLangTagForMode", () => {
  it("maps modes to BCP-47 tags", () => {
    const pref = ["bn-BD", "bn-IN", "en-IN", "en-US"];
    expect(speechLangTagForMode(SPEECH_LANG_MODES.BN, pref)).toBe("bn-BD");
    expect(speechLangTagForMode(SPEECH_LANG_MODES.EN, pref)).toBe("en-US");
    expect(speechLangTagForMode(SPEECH_LANG_MODES.BANGLISH, pref)).toBe("en-IN");
  });
});

describe("scoreTranscriptForMode", () => {
  it("prefers banglish alternative for banglish mode", () => {
    const banglish = scoreTranscriptForMode("amar kalke chuti lagbe", SPEECH_LANG_MODES.BANGLISH);
    const english = scoreTranscriptForMode("Please check my leave balance", SPEECH_LANG_MODES.BANGLISH);
    expect(banglish).toBeGreaterThan(english);
  });
});

describe("speechLangChainForMode", () => {
  it("builds mode-specific fallback chains", () => {
    expect(speechLangChainForMode(SPEECH_LANG_MODES.EN)[0]).toBe("en-US");
    expect(speechLangChainForMode(SPEECH_LANG_MODES.BANGLISH)[0]).toBe("en-IN");
    expect(speechLangChainForMode(SPEECH_LANG_MODES.BN)[0]).toBe("bn-BD");
  });
});
