"""Localized copy for the expense wizard (no LLM — structured data must stay exact)."""

from __future__ import annotations

import hashlib
from typing import Any

ReplyLang = str  # "en" | "bn" | "banglish"

_ACK_OPENINGS: dict[ReplyLang, tuple[str, ...]] = {
    "en": ("Noted for", "Got it for", "Recorded for"),
    "bn": ("নোট করেছি —", "যোগ করেছি —", "লিপিবদ্ধ —"),
    "banglish": ("Note korechi —", "Likhe rekhechi —", "Save korechi —"),
}

_MORE_LINES_EN: tuple[str, ...] = (
    "Any more lines? (e.g. `bus 50 office to home`) — or **done** / **yes** for the summary.",
    "Anything else to add? One line is fine — or type **done** when you're ready to review.",
    "More expenses today? Add a line, or say **done** to see the summary.",
)

_MORE_LINES_BN: tuple[str, ...] = (
    "আর কিছু যোগ করবেন? (যেমন: `bus 50 office to home`) — না হলে **শেষ** বা **হ্যাঁ** লিখুন।",
    "আর কোনো খরচ আছে? এক লাইন লিখুন, অথবা **শেষ** লিখে summary দেখুন।",
    "বাকি কিছু থাকলে লিখুন — শেষ করতে **শেষ** বা **হ্যাঁ** বলুন।",
)

_MORE_LINES_BANGLISH: tuple[str, ...] = (
    "Ar kono line? (e.g. `bus 50 office to home`) — na hole **shesh** / **yes** diye summary dekhen.",
    "Aro kharcha ache? Ek line likhun, ba **shesh** likhe review nen.",
    "Baki kichu thakle likhun — **shesh** dile summary asbe.",
)


def normalize_reply_lang(lang: str | None) -> ReplyLang:
    if lang in ("en", "bn", "banglish"):
        return lang
    return "bn"


def pick_rotating_phrase(phrases: tuple[str, ...], *, seed: str = "") -> str:
    """Pick a stable-but-varied phrase from a pool (same seed → same pick in one turn)."""
    if not phrases:
        return ""
    key = (seed or "default").encode("utf-8")
    idx = int(hashlib.md5(key).hexdigest(), 16) % len(phrases)
    return phrases[idx]


def format_expense_line_bullet(row: dict[str, Any], lang: ReplyLang) -> str:
    """Compact line for grouped acknowledgment (amounts/categories must stay exact)."""
    cat = str(row.get("category") or "Other")
    amt = float(row.get("amount") or 0)
    frm = str(row.get("from_location") or "").strip()
    to = str(row.get("to_location") or "").strip()
    if frm and to:
        route = f"{frm} → {to}"
        return f"**{cat}** ({route}) — **{amt:g} Tk**"
    return f"**{cat}** — **{amt:g} Tk**"


def grouped_expense_ack_header(incurred_date_iso: str, lang: ReplyLang, *, seed: str = "") -> str:
    opening = pick_rotating_phrase(_ACK_OPENINGS.get(lang, _ACK_OPENINGS["bn"]), seed=seed)
    if not incurred_date_iso:
        if lang == "en":
            return "So far:"
        if lang == "banglish":
            return "E porjonto:"
        return "এ পর্যন্ত:"
    if lang == "en":
        return f"{opening} **{incurred_date_iso}**:"
    if lang == "banglish":
        return f"{opening} **{incurred_date_iso}**:"
    return f"{opening} **{incurred_date_iso}** তারিখ:"


def lang_from_block(block: dict[str, Any] | None) -> ReplyLang:
    return normalize_reply_lang((block or {}).get("reply_language"))


def category_options_line() -> str:
    from chat.services.expense_extraction import EXPENSE_CATEGORIES

    return ", ".join(EXPENSE_CATEGORIES)


def ask_category_prompt(
    amount: float, lang: ReplyLang, *, include_lead: bool = True
) -> str:
    opts = category_options_line()
    if lang == "en":
        lead = (
            f"Got **{amount:g} Tk** — but the **category** is not clear.\n\n"
            if include_lead
            else ""
        )
        return (
            f"{lead}"
            "What was this expense for? Pick a CRM category:\n"
            f"- {opts}\n\n"
            "Example: `snack`, `other`, `bus` — one word is enough."
        )
    if lang == "banglish":
        lead = (
            f"**{amount:g} Tk** expense peyechi — kintu **category** clear na.\n\n"
            if include_lead
            else ""
        )
        return (
            f"{lead}"
            "Ei kharcha kiser jonno? CRM form onujayi category bachen:\n"
            f"- {opts}\n\n"
            "Example: `snack`, `other`, `bus` — ek word e likhlei hobe."
        )
    lead = (
        f"**{amount:g} টাকা** খরচের তথ্য পেয়েছি — কিন্তু ধরন স্পষ্ট নয়।\n\n"
        if include_lead
        else ""
    )
    return (
        f"{lead}"
        "এই খরচটি **কিসের জন্য** হয়েছে? CRM ফর্ম অনুযায়ী category বেছে নিন:\n"
        f"- {opts}\n\n"
        "উদাহরণ: `snack`, `other`, `bus` — এক শব্দেই লিখলেই হবে।"
    )


def ask_from_to_prompt(
    category: str, amount: float, lang: ReplyLang, *, include_lead: bool = True
) -> str:
    if lang == "en":
        lead = f"**{category}** — **{amount:g} Tk**.\n\n" if include_lead else ""
        return (
            f"{lead}"
            "This category needs **From** and **To** (like the CRM form).\n"
            "Write: `office theke badda` or `from office to motijheel`"
        )
    if lang == "banglish":
        lead = f"**{category}** — **{amount:g} Tk**.\n\n" if include_lead else ""
        return (
            f"{lead}"
            "Ei dhoroner kharchay **From** ar **To** lagbe (CRM form er moto).\n"
            "Likhun: `office theke badda` ba `from office to motijheel`"
        )
    lead = f"**{category}** — **{amount:g} Tk**।\n\n" if include_lead else ""
    return (
        f"{lead}"
        "এই ধরনের খরচে **From** ও **To** লাগে (যেমন স্ক্রিনশটের ফর্মে)।\n"
        "লিখুন: `office theke badda` বা `from office to motijheel`"
    )


def ask_more_lines_prompt(lang: ReplyLang, *, seed: str = "") -> str:
    if lang == "en":
        return pick_rotating_phrase(_MORE_LINES_EN, seed=seed or "more")
    if lang == "banglish":
        return pick_rotating_phrase(_MORE_LINES_BANGLISH, seed=seed or "more")
    return pick_rotating_phrase(_MORE_LINES_BN, seed=seed or "more")


def review_confirm_footer(lang: ReplyLang) -> str:
    if lang == "en":
        return (
            "Is the information above correct?\n"
            "- **Yes** — proceed to the next step\n"
            "- **No** — fix it (e.g. change bus from 50 to 70 Tk)"
        )
    if lang == "banglish":
        return (
            "Uparer tathya ki thik ache?\n"
            "- **Yes** — poroborti dhape jaben\n"
            "- **No** — thik korun (e.g. bus 50 na 70 Tk)"
        )
    return (
        "উপরের তথ্য কি ঠিক আছে?\n"
        "- **হ্যাঁ** — পরবর্তী ধাপে যাবেন\n"
        "- **না** — ঠিক করুন (যেমন: bus 50 না 70)"
    )


def review_head(incurred_date_iso: str, lang: ReplyLang) -> str:
    if lang == "en":
        base = "**Daily expense — review**"
    elif lang == "banglish":
        base = "**দৈনিক খরচ — পর্যালোচনা**"
    else:
        base = "**দৈনিক খরচ — পর্যালোচনা**"
    if incurred_date_iso:
        return f"{base} ({incurred_date_iso})"
    return base


def total_label(lang: ReplyLang) -> str:
    if lang == "en":
        return "Total"
    if lang == "banglish":
        return "মোট"
    return "মোট"


def submit_confirm_prompt(lang: ReplyLang) -> str:
    if lang == "en":
        return (
            "Data looks good.\n\n"
            "**Submit expense to CRM?**\n"
            "- **Yes** — submit\n"
            "- **No** — edit again"
        )
    if lang == "banglish":
        return (
            "Data thik ache.\n\n"
            "**Expense CRM e joma debe?**\n"
            "- **Yes** — submit korun\n"
            "- **No** — abar edit"
        )
    return (
        "ডেটা ঠিক আছে।\n\n"
        "**Expense CRM-এ জমা দেব?**\n"
        "- **হ্যাঁ** — submit করুন\n"
        "- **না** — আবার সম্পাদনা"
    )


def submitted_message(
    *,
    item_count: int,
    total: float,
    incurred_date_iso: str,
    reference_id: str,
    lang: ReplyLang,
) -> str:
    date_val = incurred_date_iso or ("today" if lang == "en" else "আজ")
    if lang == "en":
        lines = [
            "**Expense submitted successfully**",
            "",
            f"- **Date:** {date_val}",
            f"- **Lines:** {item_count} · **Total:** {total:g} Tk",
        ]
        if reference_id:
            lines.append(f"- **Reference:** `{reference_id}`")
        lines.extend(
            [
                "",
                "Final approval/reimbursement happens in your company's CRM/Finance system — "
                "this chat only submits the data.",
            ]
        )
        return "\n".join(lines)
    if lang == "banglish":
        lines = [
            "**Expense successfully joma hoyeche**",
            "",
            f"- **Date:** {date_val}",
            f"- **Line:** {item_count} ti · **Mot:** {total:g} Tk",
        ]
        if reference_id:
            lines.append(f"- **Reference:** `{reference_id}`")
        lines.extend(
            [
                "",
                "Final approval/reimbursement apnar company er CRM/Finance system e hobe — "
                "ei chat shudhu data joma ney.",
            ]
        )
        return "\n".join(lines)
    lines = [
        "**Expense সফলভাবে জমা হয়েছে**",
        "",
        f"- **তারিখ:** {date_val}",
        f"- **লাইন:** {item_count} টি · **মোট:** {total:g} Tk",
    ]
    if reference_id:
        lines.append(f"- **রেফারেন্স:** `{reference_id}`")
    lines.extend(
        [
            "",
            "চূড়ান্ত অনুমোদন/প্রতিদান আপনার কোম্পানির CRM/Finance সিস্টেমে হবে — "
            "এই চ্যাট শুধু ডেটা জমা নেয়।",
        ]
    )
    return "\n".join(lines)


def collect_start_prompt(lang: ReplyLang) -> str:
    if lang == "en":
        return "Enter today's expenses (e.g. lunch 100, bus 50 office to badda)."
    if lang == "banglish":
        return "Ajker kharcha likhun (e.g. lunch 100, bus 50 office to badda)."
    return "আজকের খরচের বিস্তারিত লিখুন (যেমন: lunch 100, bus 50)।"


def empty_collect_prompt(lang: ReplyLang | None) -> str:
    reply = normalize_reply_lang(lang)
    if reply == "en":
        return (
            "Tell me today's costs — I'll ask category if you give amount only.\n"
            "Or together: `lunch 100, bus 50 office to badda`"
        )
    if reply == "banglish":
        return (
            "Ajker cost bolun — amount dile category pore jiggesh korbo.\n"
            "Ba ek sathe: `lunch 100, bus 50 office to badda`"
        )
    return (
        "আজকের খরচ বলুন — amount দিলে পরে ধরন (lunch/bus/…) জিজ্ঞেস করব।\n"
        "অথবা একসাথে: `lunch 100, bus 50 office to badda`"
    )


def malformed_collect_prompt(lang: ReplyLang | None) -> str:
    reply = normalize_reply_lang(lang)
    if reply == "en":
        return (
            "Some lines were unclear — please include category and amount.\n"
            "Example: lunch 100, bus 50 office to home"
        )
    if reply == "banglish":
        return (
            "Kichu line bujhte parini — category ar amount clear likhun.\n"
            "Example: lunch 100, bus 50 office to home"
        )
    return (
        "কিছু লাইন বুঝতে পারিনি। category ও amount স্পষ্ট করে লিখুন।\n"
        "উদাহরণ: lunch 100, bus 50 office to home"
    )


def category_only_amount_prompt(lang: ReplyLang | None) -> str:
    reply = normalize_reply_lang(lang)
    if reply == "en":
        return "How much was it? (e.g. 100 taka)"
    if reply == "banglish":
        return "Koto taka? (e.g. 100 taka)"
    return "কত টাকা খরচ হয়েছে? (যেমন: 100 taka)"
