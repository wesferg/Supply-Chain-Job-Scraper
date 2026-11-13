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
#
# Postings often mention several numbers ("2+ years of SQL ... 6+ years of planning
# experience", "Bachelor's and 6+ years, or Master's and 4+"). What matters is the
# highest number among the *required* mentions on a bachelor's path; preferred /
# nice-to-have mentions are reported separately and never reject a job on their own.

_WORDNUM = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
            "thirteen": 13, "fourteen": 14, "fifteen": 15}
_WORDNUM_RX = re.compile(r"\b(" + "|".join(_WORDNUM) + r")\b(\s*\(\s*\d{1,2}\s*\+?\s*\))?", re.I)

_YEARS = re.compile(
    r"(?<![\d.$])(\d{1,2})(?!\.\d)\s*\+?\s*(?:plus\s*)?"
    r"(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*)?"
    r"(?:or more\s+|or greater\s+)?"
    r"(?:full[- ]time\s+|total\s+|professional\s+|relevant\s+|related\s+)?"
    r"(?:years?|yrs?)\b(?!\s*old)(?!-old)",
    re.I,
)
_EXP_CONTEXT = re.compile(
    r"experien|work(ed|ing)?\b|professional|industry|background|role|position|track record|"
    r"exposure|hands[- ]on|in (a|an|the)\b|supply chain|planning|logistics|operations|analy",
    re.I)
_NOT_REQUIREMENT = re.compile(
    r"histor|founded|anniversar|legacy|for over|over the (past|last)|past \d|last \d|\bago\b|"
    r"\bage\b|of age|years old|warranty|guarantee|\bwithin\b|per year|each year|a year\b|"
    r"year[- ]over[- ]year|years? (program|rotation|contract|term|commitment|plan|degree|"
    r"apprenticeship)|(program|rotation|contract) (of|lasting)|serving|in business|"
    r"tenure|vesting|vest over|retention|bonus",
    re.I)
_PREFERRED = re.compile(
    r"prefer|desired|desirable|nice[- ]to[- ]have|\bbonus\b|\bideal(ly)?\b|is a plus|"
    r"\ba plus\b|\bpluses\b|would be great|advantageous|strongly considered",
    re.I)
_ADV_DEGREE = re.compile(r"master|\bms\b|\bm\.s\.|\bmba\b|ph\.?\s?d|doctor|graduate degree|advanced degree", re.I)
_BACHELOR = re.compile(r"bachelor|\bbs\b|\bb\.s\.|\bba\b|\bb\.a\.|undergrad|4[- ]year degree|college degree", re.I)
_HEAD_PREF = re.compile(r"^\W*(preferred|desired|nice|bonus|pluses|plus|additional|ideal)", re.I)
_HEAD_REQ = re.compile(r"^\W*(required|requirements|minimum|basic|must|qualifications|what you|"
                       r"you have|you bring|who you are|about you|skills|experience)", re.I)


def _wordnums(text: str) -> str:
    def sub(m):
        return str(_WORDNUM[m.group(1).lower()])
    return _WORDNUM_RX.sub(sub, text)


def years_detail(text: str) -> tuple[Optional[int], Optional[int]]:
    """(required years, preferred years) from a posting; None where not stated."""
    if not text:
        return None, None
    text = _wordnums(text)
    required: list[int] = []
    preferred: list[int] = []
    section_pref = False
    for line in text.split("\n"):
        stripped = line.strip()
        if len(stripped) <= 70 and not _YEARS.search(stripped):  # looks like a heading
            if _HEAD_PREF.search(stripped):
                section_pref = True
            elif _HEAD_REQ.search(stripped):
                section_pref = False
        for sent in re.split(r"(?<=[.;!?])\s+(?=[A-Z0-9(•\-])|•", line):
            hits = list(_YEARS.finditer(sent))
            if not hits:
                continue
            sent_pref = section_pref or bool(_PREFERRED.search(sent))
            for i, m in enumerate(hits):
                n = int(m.group(1))
                if n > 20:
                    continue
                before = sent[max(0, m.start() - 70):m.start()]
                after = sent[m.end():m.end() + 90]
                # Only the words between this number and the next one belong to it.
                if i + 1 < len(hits):
                    after = sent[m.end():min(hits[i + 1].start(), m.end() + 90)]
                ctx = before[-40:] + " " + after
                if _NOT_REQUIREMENT.search(after[:40]) or _NOT_REQUIREMENT.search(before[-25:]):
                    continue
                if not _EXP_CONTEXT.search(after) and not re.search(r"experience", before[-60:], re.I):
                    continue
                # "...or a Master's degree and 4+ years": not the bachelor's path
                prev_end = hits[i - 1].end() if i else 0
                lead = sent[max(prev_end, m.start() - 60):m.start()]
                if _ADV_DEGREE.search(lead) and not _BACHELOR.search(lead):
                    continue
                pref = sent_pref or bool(_PREFERRED.search(ctx))
                (preferred if pref else required).append(n)
    return (max(required) if required else None, max(preferred) if preferred else None)


def required_years(text: str) -> Optional[int]:
    """Years of experience the posting requires on a bachelor's path, or None."""
    return years_detail(text)[0]


# ---------- posting dates ----------

def posted_date(raw, today=None):
    """Normalise whatever an ATS gives ("2026-11-09", "2026-11-09T18:00Z", epoch ms,
    "Posted 3 Days Ago", "Posted 30+ Days Ago") to a date, or None if unknown."""
    from datetime import date, datetime, timedelta, timezone
    today = today or date.today()
    if raw in (None, ""):
        return None
    s = str(raw).strip()
    if re.fullmatch(r"\d{12,13}", s):
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc).date()
    if re.fullmatch(r"\d{9,10}", s):
        return datetime.fromtimestamp(int(s), tz=timezone.utc).date()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    low = s.lower()
    if "today" in low or "just posted" in low or "hour" in low:
        return today
    if "yesterday" in low:
        return today - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*days?", low)
    if m:
        return today - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\+?\s*(weeks?|months?)", low)
    if m:
        return today - timedelta(days=int(m.group(1)) * (7 if "week" in m.group(2) else 30))
    return None
