"""Language guard for expense clarify polish (P2 — prevent EN flip)."""

from __future__ import annotations

from chat.services.translator import (
    has_banglish_words,
    has_bengali_chars,
)


def clarify_polish_language_ok(
    target_lang: str,
    polished: str,
    *,
    template: str = "",
) -> bool:
    """
    Reject polished clarify text that flipped away from the locked reply language.

    ``target_lang``: bn | banglish | en (from expense block).
    """
    text = (polished or "").strip()
    ref = (template or "").strip()
    if not text:
        return False
    lang = (target_lang or "bn").strip().lower()

    if lang == "en":
        if has_bengali_chars(text) and not has_bengali_chars(ref):
            return False
        return True

    # bn or banglish — must not become English-only when template was Bengali/Banglish
    polished_has_bn = has_bengali_chars(text)
    polished_has_banglish = has_banglish_words(text)
    ref_has_bn = has_bengali_chars(ref)
    ref_has_banglish = has_banglish_words(ref)

    if lang == "bn":
        if not polished_has_bn and ref_has_bn:
            return False
        if not polished_has_bn and not polished_has_banglish and (ref_has_bn or ref_has_banglish):
            return False
        return True

    # banglish — allow Roman + optional Bengali script; block pure English rewrite
    if lang == "banglish":
        if polished_has_bn or polished_has_banglish:
            return True
        if ref_has_bn or ref_has_banglish:
            return False
    return True
