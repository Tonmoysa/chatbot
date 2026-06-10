"""Bengali digit and month normalization for rule-based parsers."""

from __future__ import annotations

import re
from datetime import date

_BN_DIGIT_MAP = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

_BN_MONTHS: dict[str, int] = {
    "জানুয়ারি": 1,
    "জানু": 1,
    "ফেব্রুয়ারি": 2,
    "ফেব": 2,
    "মার্চ": 3,
    "এপ্রিল": 4,
    "মে": 5,
    "জুন": 6,
    "জুলাই": 7,
    "আগস্ট": 8,
    "সেপ্টেম্বর": 9,
    "অক্টোবর": 10,
    "নভেম্বর": 11,
    "ডিসেম্বর": 12,
}


def normalize_bn_digits(text: str) -> str:
    return (text or "").translate(_BN_DIGIT_MAP)


def normalize_message_for_parsing(message: str) -> str:
    return normalize_bn_digits(message or "")


def _resolve_next_calendar_date(month_num: int, day: int, *, today: date) -> date | None:
    for y in range(today.year, today.year + 3):
        try:
            d = date(y, month_num, day)
        except ValueError:
            continue
        if d >= today:
            return d
    return None


def _all_bn_calendar_dates(message: str, *, today: date | None = None) -> list[date]:
    """Return all resolved calendar dates in message order (BN + EN month names)."""
    today_d = today or date.today()
    raw = normalize_bn_digits((message or "").strip())
    if not raw:
        return []
    low = raw.lower()
    found: list[tuple[int, date]] = []

    for month_bn, month_num in _BN_MONTHS.items():
        for m in re.finditer(
            rf"(?:agami|agamikal|আগামী|আগামীকাল|next)?\s*(\d{{1,2}})\s*{re.escape(month_bn)}",
            raw,
            re.I | re.UNICODE,
        ):
            try:
                dnum = int(m.group(1))
            except ValueError:
                continue
            resolved = _resolve_next_calendar_date(month_num, dnum, today=today_d)
            if resolved:
                found.append((m.start(), resolved))

    for m_en in re.finditer(
        r"(?:agami|agamikal|আগামী|next)?\s*(\d{1,2})\s+(june|jun|july|jul|may|march|mar|april|apr|august|aug|september|sept|sep|october|oct|november|nov|december|dec|january|jan|february|feb)",
        low,
    ):
        from chat.services.entity_extractor import _MONTH_MAP

        mon = _MONTH_MAP.get(m_en.group(2), 0)
        if not mon:
            continue
        try:
            dnum = int(m_en.group(1))
        except ValueError:
            continue
        resolved = _resolve_next_calendar_date(mon, dnum, today=today_d)
        if resolved:
            found.append((m_en.start(), resolved))

    found.sort(key=lambda x: x[0])
    out: list[date] = []
    seen: set[str] = set()
    for _, d in found:
        iso = d.isoformat()
        if iso not in seen:
            seen.add(iso)
            out.append(d)
    return out


def infer_bn_calendar_date_range(
    message: str, *, today: date | None = None
) -> tuple[str, str] | None:
    """
    Parse Bengali date ranges: ``২০ জুন থেকে ২২ জুন পর্যন্ত``.
    Returns (start_iso, end_iso) or None.
    """
    raw = normalize_bn_digits((message or "").strip())
    if not raw:
        return None
    if not re.search(
        r"থেকে|পর্যন্ত|theke|porjonto|from|to|until|till",
        raw,
        re.I | re.UNICODE,
    ):
        return None
    dates = _all_bn_calendar_dates(raw, today=today)
    if len(dates) < 2:
        return None
    start_d, end_d = dates[0], dates[-1]
    if end_d < start_d:
        start_d, end_d = end_d, start_d
    return start_d.isoformat(), end_d.isoformat()


def infer_bn_calendar_date(message: str, *, today: date | None = None) -> str | None:
    """
    Parse Bengali calendar phrases: আগামী ১৫ জুন, ১৬ জুন, 15 june.
    Returns ISO date or None.
    """
    today_d = today or date.today()
    raw = normalize_bn_digits((message or "").strip())
    if not raw:
        return None
    low = raw.lower()

    for month_bn, month_num in _BN_MONTHS.items():
        m = re.search(
            rf"(?:agami|agamikal|আগামী|আগামীকাল|next)?\s*(\d{{1,2}})\s*{re.escape(month_bn)}",
            raw,
            re.I | re.UNICODE,
        )
        if not m:
            m = re.search(
                rf"(\d{{1,2}})\s*{re.escape(month_bn)}",
                raw,
                re.I | re.UNICODE,
            )
        if m:
            try:
                dnum = int(m.group(1))
            except ValueError:
                continue
            resolved = _resolve_next_calendar_date(month_num, dnum, today=today_d)
            if resolved:
                return resolved.isoformat()

    m_en = re.search(
        r"(?:agami|agamikal|আগামী|next)?\s*(\d{1,2})\s+(june|jun|july|jul|may|march|mar|april|apr|august|aug|september|sept|sep|october|oct|november|nov|december|dec|january|jan|february|feb)",
        low,
    )
    if m_en:
        from chat.services.entity_extractor import _MONTH_MAP

        mon = _MONTH_MAP.get(m_en.group(2), 0)
        if mon:
            try:
                dnum = int(m_en.group(1))
            except ValueError:
                return None
            resolved = _resolve_next_calendar_date(mon, dnum, today=today_d)
            if resolved:
                return resolved.isoformat()
    return None
