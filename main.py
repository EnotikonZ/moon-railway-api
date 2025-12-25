import os
import re
from datetime import date, datetime, timedelta
from typing import Optional, Tuple, Dict, Any

import requests
from fastapi import FastAPI, Query, HTTPException
from cachetools import TTLCache

# ----------------------------
# CONFIG
# ----------------------------

RAMBLER_URL = "https://horoscopes.rambler.ru/moon/calendar/{calendar_date}/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "close",
}

TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))

# Cache: key = (date_str), value = dict with parsed info
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", str(60 * 60 * 24)))  # 24h
cache = TTLCache(maxsize=2000, ttl=CACHE_TTL_SECONDS)

# Regex patterns for Rambler common phrasing:
# "До 11:42 — 6-й лунный день"
# "После 11:42 — 7-й лунный день"
RE_UNTIL = re.compile(
    r"До\s+(?P<time>\d{1,2}:\d{2}).{0,80}?(?P<day>\d{1,2})[-\s]*й\s+лунн",
    re.IGNORECASE
)
RE_AFTER = re.compile(
    r"После\s+(?P<time>\d{1,2}:\d{2}).{0,80}?(?P<day>\d{1,2})[-\s]*й\s+лунн",
    re.IGNORECASE
)

# Fallback: just any "N-й лунный день"
RE_ANY_LUNAR_DAY = re.compile(r"(?P<day>\d{1,2})[-\s]*й\s+лунн", re.IGNORECASE)

# ----------------------------
# APP
# ----------------------------

app = FastAPI(
    title="Lunar Day API (Rambler)",
    version="1.0.0"
)


# ----------------------------
# HELPERS
# ----------------------------

def fetch_page_text(d: date) -> str:
    """Fetch Rambler page text for given date, return cleaned text."""
    date_str = d.isoformat()

    # cache raw text (optional): we cache parsed transition, so no need
    url = RAMBLER_URL.format(calendar_date=date_str)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Timeout fetching Rambler")
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Request error: {e}")

    if resp.status_code != 200:
        # Rambler sometimes returns 403/429 on blocks
        raise HTTPException(status_code=502, detail=f"Rambler returned status {resp.status_code}")

    # We use .text to decode properly (requests handles encoding)
    html_text = resp.text
    resp.close()

    # Convert HTML -> plain text quickly without bs4:
    # We'll strip tags rough with regex to keep dependencies small
    # (bs4 is ok too, but regex is enough for our patterns)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


def extract_transition(d: date) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """
    Return (transition_time, before_day, after_day)
    - If day changes within the date: time="11:42", before=6, after=7
    - If no change found: (None, None, day)
    """
    date_str = d.isoformat()
    cache_key = ("transition", date_str)
    if cache_key in cache:
        return cache[cache_key]

    text = fetch_page_text(d)

    m_until = RE_UNTIL.search(text)
    m_after = RE_AFTER.search(text)

    if m_until and m_after:
        t1 = m_until.group("time")
        t2 = m_after.group("time")
        t = t1 if t1 == t2 else t1  # usually same
        before_day = int(m_until.group("day"))
        after_day = int(m_after.group("day"))
        result = (t, before_day, after_day)
        cache[cache_key] = result
        return result

    # If no transition phrases found, try fallback: any lunar day number
    m_any = RE_ANY_LUNAR_DAY.search(text)
    if m_any:
        day_num = int(m_any.group("day"))
        result = (None, None, day_num)
        cache[cache_key] = result
        return result

    # Could be blocked page or markup changed
    raise HTTPException(status_code=502, detail="Could not parse Rambler page (blocked or markup changed)")


def format_ru_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def build_two_lines(d: date) -> Dict[str, Any]:
    """
    Build the final text for date d:
    line1: previous lunar day from yesterday transition -> today transition
    line2: current lunar day from today transition -> "до следующего дня"
    """

    t_today, before_today, after_today = extract_transition(d)

    # If no transition today, return a single-line fallback
    if not t_today or before_today is None or after_today is None:
        day_num = after_today if after_today is not None else before_today
        if day_num is None:
            raise HTTPException(status_code=502, detail="Could not determine lunar day number")
        text = f"{day_num} лунный день {format_ru_date(d)} (без смены в течение суток)"
        return {
            "date": d.isoformat(),
            "transition_time": None,
            "before_day": None,
            "after_day": day_num,
            "line1": text,
            "line2": None,
            "text": text,
        }

    # Yesterday transition gives us the start time for the "before_today" day
    d_prev = d - timedelta(days=1)
    t_prev, before_prev, after_prev = extract_transition(d_prev)

    # Ideally: after_prev == before_today
    # But sometimes parsing differs; we still use t_prev as start time.
    start_time = t_prev if t_prev else "00:00"
    line1 = (
        f"{before_today} лунный день c {start_time} {format_ru_date(d_prev)} "
        f"по {t_today} {format_ru_date(d)}"
    )
    line2 = (
        f"{after_today} лунный день c {t_today} {format_ru_date(d)} "
        f"и до следующего дня"
    )

    return {
        "date": d.isoformat(),
        "transition_time": t_today,
        "before_day": before_today,
        "after_day": after_today,
        "line1": line1,
        "line2": line2,
        "text": line1 + "\n" + line2,
    }


# ----------------------------
# ROUTES
# ----------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/lunar-text")
def lunar_text(
    d: date = Query(..., description="Date in YYYY-MM-DD"),
):
    """
    Returns:
    {
      "text": "6 лунный день ...\n7 лунный день ...",
      ...metadata
    }
    """
    return build_two_lines(d)


@app.get("/lunar-string")
def lunar_string(
    d: date = Query(..., description="Date in YYYY-MM-DD"),
):
    """
    Returns plain string (useful for Lovable as one text field).
    """
    result = build_two_lines(d)
    return result["text"]
