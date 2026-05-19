import { describe, expect, it } from "vitest";
import {
  canUseSpeechProvider,
  normalizeTranscript,
  speechErrorMessage,
} from "./speechUtils.js";

describe("normalizeTranscript", () => {
  it("trims and collapses whitespace", () => {
    expect(normalizeTranscript("  hello   world  ")).toBe("hello world");
  });

  it("preserves Bengali Unicode", () => {
    expect(normalizeTranscript("আমার   ছুটি")).toBe("আমার ছুটি");
  });

  it("handles Banglish mixed text", () => {
    expect(normalizeTranscript("amar  leave balance koto")).toBe("amar leave balance koto");
  });
});

describe("speechErrorMessage", () => {
  it("maps not-allowed to user-friendly text", () => {
    expect(speechErrorMessage({ error: "not-allowed" })).toMatch(/denied/i);
  });
});

describe("canUseSpeechProvider", () => {
  it("reports webSpeech based on constructor", () => {
    const had = globalThis.webkitSpeechRecognition;
    globalThis.webkitSpeechRecognition = function Mock() {};
    expect(canUseSpeechProvider("webSpeech")).toBe(true);
    if (had) globalThis.webkitSpeechRecognition = had;
    else delete globalThis.webkitSpeechRecognition;
  });
});
