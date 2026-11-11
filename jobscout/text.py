"""Text helpers: HTML -> plain text, salary parsing, years-of-experience parsing."""
from __future__ import annotations

import html
import re
from typing import Optional

_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|h\d|tr)[^>]*>", re.I)
_WS = re.compile(r"[ \t\r\f\v]+")


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)          # Greenhouse double-escapes its HTML
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


# ---------- salary ----------

_NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kK])?"
_RANGE = re.compile(
    r"(?:\$|USD\s*)\s*" + _NUM +
    r"\s*(?:-|–|—|to)\s*(?:\$|USD\s*)?\s*" + _NUM +
    r"(?:\s*(?:USD|usd))?\s*(?:(?:/|per|an|a)\s*(hour|hr|year|yr|annum|annually))?",
)
_SINGLE = re.compile(
    r"(?:\$|USD\s*)\s*" + _NUM + r"\s*(?:(?:/|per|an|a)\s*(hour|hr|year|yr|annum))"
)


def _to_num(raw: str, k: Optional[str]) -> float:
    v = float(raw.replace(",", ""))
    if k:
        v *= 1000
    return v


def _annualize(lo: float, hi: float, unit: Optional[str]) -> tuple[float, float]:
    hourly = (unit or "").lower() in ("hour", "hr") or hi < 300
    if hourly:
        return lo * 2080, hi * 2080
    return lo, hi


def parse_salary(text: str) -> tuple[Optional[float], Optional[float]]:
    """Find the first plausible pay range in a posting. Returns annual (min, max)."""
    if not text:
        return None, None
    for m in _RANGE.finditer(text):
        lo = _to_num(m.group(1), m.group(2))
        hi = _to_num(m.group(3), m.group(4))
        # "$71K - $110" style: carry the K across
        if m.group(2) and not m.group(4) and hi < 1000:
            hi *= 1000
        if hi < lo:
            lo, hi = hi, lo
        lo, hi = _annualize(lo, hi, m.group(5))
        if 20_000 <= lo <= 1_000_000 and 20_000 <= hi <= 1_500_000:
            return lo, hi
    m = _SINGLE.search(text)
    if m:
        v = _to_num(m.group(1), m.group(2))
        v, _ = _annualize(v, v, m.group(3))
        if 20_000 <= v <= 1_000_000:
            return v, v
    return None, None


# ---------- years of experience ----------

_YEARS = re.compile(
    r"(?:minimum of |at least |min\.? )?"
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?)?\s*"
    r"(?:or more\s+)?years?['’]?\s*(?:of\s+)?"
    r"(?=([^.\n;]{0,80}))",  # lookahead, so "8+ yrs ..., or 5+ yrs" yields both
    re.I,
)
_EXP_WORDS = re.compile(r"experience|work(ing)? in|professional|industry|related", re.I)
_NOT_EXP = re.compile(r"\bage\b|\bold\b|of age|warranty|history|anniversary|founded|company", re.I)


def required_years(text: str) -> Optional[int]:
    """Smallest 'N years of experience' requirement mentioned, or None.

    Takes the minimum across mentions so "8+ years, or 5+ with a Master's"
    reads as 5, and "Bachelor's + 2 years" reads as 2.
    """
    if not text:
        return None
    found: list[int] = []
    for m in _YEARS.finditer(text):
        tail = m.group(3) or ""
        if not _EXP_WORDS.search(tail) or _NOT_EXP.search(tail):
            continue
        n = int(m.group(1))
        if 0 <= n <= 15:
            found.append(n)
    return min(found) if found else None
