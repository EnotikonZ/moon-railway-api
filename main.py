import os
import re
import logging
from datetime import date, timedelta
from typing import Optional, Tuple, Dict, Any

import requests
from fastapi import FastAPI, Query, HTTPException
from cachetools import TTLCache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------
# LOGGING
# ----------------------------

logger = logging.getLogger("moon_api")
logging.basicConfig(level=logging.INFO)

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
    "Referer": "https://horoscopes.rambler.ru/",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "close",
}

TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", str(60 * 60 * 24)))  # 24h
cache = TTLCache(maxsize=2000, ttl=CACHE_TTL_SECONDS)

# Regex patterns for Rambler common phrasing:
RE_UNTIL = re.compile(
    r"До\s+(?P<time>\d{1,2}:\d{2}).{0,120}?(?P<day>\d{1,2})[-\s]*й\s+лунн",
    re.IGNORECASE
)
RE_AFTER = re.compile(
    r"После\s+(?P<time>\d{1,2}:\d{2}).{0,120}?(?P<day>\d{1,2})[-\s]*й\s+лунн",
    re.IGNORECASE
)

RE_ANY_LUNAR_DAY = re.compile(r"(?P<day>\d{1,2})[-\s]*й\s+лунн", re.IGNORECASE)

# ----------------------------
# HTTP SESSION WITH RETRIES
# ----------------------------

def _make_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=3,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


SESSION = _make_session()

# ----------------------------
# APP
# ----------------------------

app = FastAPI(
    title="Lunar Day API (Rambler)",
    version="1.1.0"
)

# ----------------------------
# HELPERS
# ----------------------------

def fetch_page_text(d: date) -> str:
    """
    Fetch Rambler page HTML and convert to plain text.
    Also logs status code and a text sample for debugging.
    """
    date_str = d.isoformat()
    url = RAMBLER_URL.format(calendar_date=date_str)

    try:
        resp = SESSION.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.exceptions.Timeout:
        logger.exception("Timeout while fetching Rambler for %s", date_str)
        raise HTTPException(status_code=504, detail="Timeout fetching Rambler")
    except requests.RequestException as e:
        logger.exception("Request error while fetching Rambler for %s: %s", date_str, str(e))
        raise HTTPException(status_code=502, detail=f"Request error: {e}")

    status = resp.status_code
    html_text = resp.text or ""
    resp.close()

    logger.info("Rambler fetch %s -> status=%s html_len=%s", url, status, len(html_text))

    if status != 200:
        # Log a short html sample (may show block/captcha)
        sample = re.sub(r"\s+", " ", html_text[:1500]).strip()
        logger.warning("Non-200 from Rambler. status=%s sample=%s", status, sample)
        raise HTTPException(status_code=502, detail=f"Rambler returned status {status}")

    # Remove scripts/styles
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)

    # Strip tags
    text = re.sub(r"<[^>]+>", " ", text)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Debug sample: helpful to see what we got
    logger.info("Text sample for %s: %s", date_str, text[:500])

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
        t = t1 if t1 == t2 else t1
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

    # Log text excerpt so we can adjust regex
    logger.warning("Could not parse transition for %s. Text excerpt: %s", date_str, text[:800])
    raise HTTPException(status_code=502, detail="Could not parse Rambler page (blocked or markup changed)")


def format_ru_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def build_two_lines(d: date) -> Dict[str, Any]:
    """
    Build:
    line1: previous lunar day from yesterday transition -> today transition
    line2: current lunar day from today transition -> "до следующего дня"
    """
    t_today, before_today, after_today = extract_transition(d)

    if not t_today or before_today is None or after_today is None:
        # no transition in this date
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

    d_prev = d - timedelta(days=1)
    t_prev, _, _ = extract_transition(d_prev)

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
    return build_two_lines(d)


@app.get("/lunar-string")
def lunar_string(
    d: date = Query(..., description="Date in YYYY-MM-DD"),
):
    result = build_two_lines(d)
    return result["text"]


@app.get("/debug-raw")
def debug_raw(
    d: date = Query(..., description="Date in YYYY-MM-DD"),
    n: int = Query(2000, description="How many chars to return"),
):
    """
    Returns first N chars of cleaned text for debugging parsing.
    Remove this endpoint later.
    """
    txt = fetch_page_text(d)
    return {
        "date": d.isoformat(),
        "len": len(txt),
        "sample": txt[:n],
    }
