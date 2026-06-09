"""Clarify prompt copy — variant intros/footers (P2 outbound UX)."""

from __future__ import annotations

from dataclasses import dataclass

from chat.services.expense_copy import normalize_reply_lang

_CLARIFY_INTRO_INITIAL = {
    "bn": [
        "পর্যালোচনার আগে কিছু তথ্য নিশ্চিত করতে হবে:\n",
        "রিভিউতে যাওয়ার আগে দুটো ছোট বিষয় clear করতে হবে:\n",
        "খরচ জমা দেওয়ার আগে নিচের তথ্য দয়া করে confirm করুন:\n",
    ],
    "banglish": [
        "Review er age kichu info confirm korte hobe:\n",
        "Joma dewar age dui ta choto bisoy clear korte hobe:\n",
        "Expense review te jaowar age niche confirm korun:\n",
    ],
    "en": [
        "A few details need confirming before review:\n",
        "Before we show the review screen, please confirm:\n",
        "Almost ready to review — just need your input on:\n",
    ],
}

_CLARIFY_INTRO_FOLLOWUP = {
    "bn": [
        "ধন্যবাদ — কিছু item এখনো confirm করতে হবে:\n",
        "একটা ঠিক হয়েছে — বাকি item গুলো confirm করুন:\n",
        "আংশিক উত্তর পেয়েছি — নিচের item এখনো open:\n",
    ],
    "banglish": [
        "Dhonnobad — kichu item ekhono confirm korte hobe:\n",
        "Ekta part clear — baki item gulo confirm korun:\n",
        "Partial answer peyechi — niche open item ache:\n",
    ],
    "en": [
        "Thanks — a few items still need your input:\n",
        "Got part of that — still open:\n",
        "Partially confirmed — please answer the remaining item(s):\n",
    ],
}

_CLARIFY_INTRO_DISAMBIGUATION = {
    "bn": [
        "দুইটা item-এ উত্তর দরকার — আপনি **কোনটি** বদলাচ্ছেন?\n\n",
        "একাধিক item open — **কোন number**-এর উত্তর দিচ্ছেন?\n\n",
    ],
    "banglish": [
        "Dui ta item e answer dorkar — **kon ta** modify korchen?\n\n",
        "Onek gula open — **kon number** er answer dilen?\n\n",
    ],
    "en": [
        "Two items need input — which one are you answering?\n\n",
        "Multiple items are open — which **number** do you mean?\n\n",
    ],
}

_CLARIFY_FOOTER = {
    "bn": [
        "\nএক মেসেজে উত্তর দিন (যেমন: `mirpur, snack` বা `yes, lunch`)।",
        "\nএক লাইনে উত্তর দিন — যেমন: `2 metro rail` বা `ha, lunch`।",
    ],
    "banglish": [
        "\nEk message e answer din (e.g. `mirpur, snack` ba `yes, lunch`).",
        "\nNumber diye likhte paren — e.g. `2 metro rail` ba `1 yes`.",
    ],
    "en": [
        "\nReply in one message (e.g. `mirpur, snack` or `yes, lunch`).",
        "\nYou can use a number — e.g. `2 metro rail` or `1 yes`.",
    ],
}

_DISAMBIG_FOOTER = {
    "bn": "\nনম্বর দিয়ে লিখুন — যেমন: **`2 metro rail`** বা **`1 yes`**।",
    "banglish": "\nNumber diye likhun — e.g. **`2 metro rail`** ba **`1 yes`**.",
    "en": "\nReply with the **number** and answer, e.g. **`2 metro rail`** or **`1 yes`**.",
}


@dataclass(frozen=True)
class ClarifyPromptContext:
    variant: str = "initial"  # initial | followup | disambiguation
    total_issues: int = 0
    resolved_count: int = 0
    open_count: int = 0


def _pick_variant(options: list[str], seed: int) -> str:
    if not options:
        return ""
    return options[seed % len(options)]


def clarify_intro(
    *,
    lang: str | None,
    context: ClarifyPromptContext | None = None,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    ctx = context or ClarifyPromptContext()
    seed = max(0, ctx.total_issues) + max(0, ctx.resolved_count) * 3
    if ctx.variant == "followup":
        pool = _CLARIFY_INTRO_FOLLOWUP.get(reply_lang, _CLARIFY_INTRO_FOLLOWUP["bn"])
    elif ctx.variant == "disambiguation":
        pool = _CLARIFY_INTRO_DISAMBIGUATION.get(
            reply_lang, _CLARIFY_INTRO_DISAMBIGUATION["bn"]
        )
    else:
        pool = _CLARIFY_INTRO_INITIAL.get(reply_lang, _CLARIFY_INTRO_INITIAL["bn"])
    return _pick_variant(pool, seed)


def clarify_footer(
    *,
    lang: str | None,
    variant: str = "initial",
    seed: int = 0,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    if variant == "disambiguation":
        return _DISAMBIG_FOOTER.get(reply_lang, _DISAMBIG_FOOTER["bn"])
    pool = _CLARIFY_FOOTER.get(reply_lang, _CLARIFY_FOOTER["bn"])
    return _pick_variant(pool, seed)
