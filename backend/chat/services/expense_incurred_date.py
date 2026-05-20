"""
Expense incurred date for policy + dedupe.

Policy: today's expense is for today; submit a future day's expense on that day
(not early via chat auto-approve).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).split("T")[0]).date()
    except Exception:
        return None


def _parse_numeric_dates_in_message(message: str, *, today: date) -> date | None:
    m_iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", message)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        except Exception:
            pass
    m_dmy = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", message)
    if m_dmy:
        d1, m1, y1 = m_dmy.group(1), m_dmy.group(2), m_dmy.group(3)
        yy = int(y1)
        if yy < 100:
            yy += 2000
        try:
            return date(yy, int(m1), int(d1))
        except Exception:
            pass
    return None


def infer_expense_incurred_date_iso(
    *,
    message: str,
    hints: dict[str, Any],
    today: date | None = None,
) -> str:
    """
    Calendar date (YYYY-MM-DD) the expense is for.
    - Explicit dates in text win.
    - Banglish / EN today vs tomorrow.
    - Else hints from upstream (e.g. LLM date / expense_incurred_date).
    - Else today (same-day claim policy default).
    """
    today_d = today or date.today()
    low = (message or "").lower()

    explicit = _parse_numeric_dates_in_message(message or "", today=today_d)
    if explicit:
        return explicit.isoformat()

    tomorrow_re = re.compile(
        r"(আগামীকাল|"
        r"\b(kalke|kalker|kal\s+er|porer\s+din|next\s+day|"
        r"tomorrow|tomarrow|tommorow|tommorrow|tomorow|tmrw|tmw)\b)",
        re.I,
    )
    today_re = re.compile(
        r"(আজ(কে)?|"
        r"\b(ajke|ajker|aaj|aajke|today)\b)",
        re.I,
    )

    if today_re.search(message) or today_re.search(low):
        return today_d.isoformat()
    if tomorrow_re.search(message) or tomorrow_re.search(low):
        return (today_d + timedelta(days=1)).isoformat()

    for key in ("expense_incurred_date", "date"):
        v = hints.get(key)
        d = _parse_iso_date(str(v) if v is not None else "")
        if d:
            return d.isoformat()

    return today_d.isoformat()
