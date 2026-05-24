"""Localized copy for the expense wizard (no LLM — structured data must stay exact)."""

from __future__ import annotations

from typing import Any

ReplyLang = str  # "en" | "bn" | "banglish"


def normalize_reply_lang(lang: str | None) -> ReplyLang:
    if lang in ("en", "bn", "banglish"):
        return lang
    return "bn"


def lang_from_block(block: dict[str, Any] | None) -> ReplyLang:
    return normalize_reply_lang((block or {}).get("reply_language"))


def category_options_line() -> str:
    from chat.services.expense_extraction import EXPENSE_CATEGORIES

    return ", ".join(EXPENSE_CATEGORIES)


def ask_category_prompt(amount: float, lang: ReplyLang) -> str:
    opts = category_options_line()
    if lang == "en":
        return (
            f"Got **{amount:g} Tk** — but the **category** is not clear.\n\n"
            "What was this expense for? Pick a CRM category:\n"
            f"- {opts}\n\n"
            "Example: `snack`, `other`, `bus` — one word is enough."
        )
    if lang == "banglish":
        return (
            f"**{amount:g} Tk** expense peyechi — kintu **category** clear na.\n\n"
            "Ei kharcha kiser jonno? CRM form onujayi category bachen:\n"
            f"- {opts}\n\n"
            "Example: `snack`, `other`, `bus` — ek word e likhlei hobe."
        )
    return (
        f"**{amount:g} টাকা** খরচের তথ্য পেয়েছি — কিন্তু ধরন স্পষ্ট নয়।\n\n"
        "এই খরচটি **কিসের জন্য** হয়েছে? CRM ফর্ম অনুযায়ী category বেছে নিন:\n"
        f"- {opts}\n\n"
        "উদাহরণ: `snack`, `other`, `bus` — এক শব্দেই লিখলেই হবে।"
    )


def ask_from_to_prompt(category: str, amount: float, lang: ReplyLang) -> str:
    if lang == "en":
        return (
            f"**{category}** — **{amount:g} Tk**.\n\n"
            "This category needs **From** and **To** (like the CRM form).\n"
            "Write: `office theke badda` or `from office to motijheel`"
        )
    if lang == "banglish":
        return (
            f"**{category}** — **{amount:g} Tk**.\n\n"
            "Ei dhoroner kharchay **From** ar **To** lagbe (CRM form er moto).\n"
            "Likhun: `office theke badda` ba `from office to motijheel`"
        )
    return (
        f"**{category}** — **{amount:g} Tk**।\n\n"
        "এই ধরনের খরচে **From** ও **To** লাগে (যেমন স্ক্রিনশটের ফর্মে)।\n"
        "লিখুন: `office theke badda` বা `from office to motijheel`"
    )


def ask_more_lines_prompt(lang: ReplyLang) -> str:
    if lang == "en":
        return (
            "Any more expenses? You can write one line (e.g. bus 50 office to home).\n"
            "If not, type **done** or **yes** to see the summary."
        )
    if lang == "banglish":
        return (
            "Ar kono kharcha ache? Ek line e likhte parben (e.g. bus 50 office to home).\n"
            "Na thakle **shesh** ba **yes** likhe summary dekhen."
        )
    return (
        "আর কোনো খরচ আছে? এক লাইনে লিখতে পারেন (যেমন: bus 50 office to home)।\n"
        "না থাকলে **শেষ** বা **হ্যাঁ** লিখে summary দেখুন।"
    )


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
        base = "**Daily expense — review**"
    else:
        base = "**দৈনিক খরচ — পর্যালোচনা**"
    if incurred_date_iso:
        return f"{base} ({incurred_date_iso})"
    return base


def total_label(lang: ReplyLang) -> str:
    if lang == "en":
        return "Total"
    if lang == "banglish":
        return "Mot"
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
