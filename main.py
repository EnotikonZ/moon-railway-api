import os
import re
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Any, List, Optional

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
cache = TTLCache(maxsize=3000, ttl=CACHE_TTL_SECONDS)

# Moscow timezone fixed (UTC+3)
MSK = timezone(timedelta(hours=3))
TZ_NAME = "Europe/Moscow"

RU_MONTH = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

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
    title="Lunar Day API (Rambler, MSK)",
    version="3.0.0"
)

# ----------------------------
# PARSING
# ----------------------------

# Пример строки:
# "6 лунный день 24 декабря 11:35 — 25 декабря 11:42"
# "7 лунный день Рыбы 25 декабря 11:42 — 26 декабря 11:49"
RE_INTERVAL = re.compile(
    r"(?P<day>\d{1,2})\s+лунный\s+день"
    r"(?:\s+(?P<zodiac>[А-Яа-яЁё]+))?"
    r"\s+(?P<d1>\d{1,2})\s+(?P<m1>[А-Яа-яЁё]+)\s+(?P<t1>\d{1,2}:\d{2})"
    r"\s*[—-]\s*"
    r"(?P<d2>\d{1,2})\s+(?P<m2>[А-Яа-яЁё]+)\s+(?P<t2>\d{1,2}:\d{2})",
    re.IGNORECASE
)

def fetch_page_text(d: date) -> str:
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
        sample = re.sub(r"\s+", " ", html_text[:1500]).strip()
        logger.warning("Non-200 from Rambler. status=%s sample=%s", status, sample)
        raise HTTPException(status_code=502, detail=f"Rambler returned status {status}")

    text = re.sub(r"<script[^>]*>.*?</script>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


def _month_num(month_ru: str) -> int:
    m = RU_MONTH.get(month_ru.lower())
    if not m:
        raise HTTPException(status_code=502, detail=f"Unknown month word: {month_ru}")
    return m


def _parse_dt(year: int, day: int, month_ru: str, time_str: str) -> datetime:
    """
    Build timezone-aware datetime in MSK from components.
    """
    m = _month_num(month_ru)
    hh, mm = time_str.split(":")
    return datetime(year, m, int(day), int(hh), int(mm), tzinfo=MSK)


def extract_intervals(d: date) -> List[Dict[str, Any]]:
    """
    Returns 1-2 intervals with ISO datetimes.
    """
    date_str = d.isoformat()
    cache_key = ("intervals_iso", date_str)
    if cache_key in cache:
        return cache[cache_key]

    text = fetch_page_text(d)
    matches = list(RE_INTERVAL.finditer(text))

    if not matches:
        logger.warning("Could not find lunar intervals for %s. Excerpt: %s", date_str, text[:1200])
        raise HTTPException(status_code=502, detail="Could not parse Rambler page (blocked or markup changed)")

    intervals: List[Dict[str, Any]] = []
    for m in matches[:4]:
        day_num = int(m.group("day"))
        zodiac = m.group("zodiac")
        if zodiac:
            zodiac = zodiac.strip()

        start_dt = _parse_dt(d.year, int(m.group("d1")), m.group("m1"), m.group("t1"))
        end_dt = _parse_dt(d.year, int(m.group("d2")), m.group("m2"), m.group("t2"))

        intervals.append({
            "day": day_num,
            "zodiac": zodiac,
            "startIso": start_dt.isoformat(),
            "endIso": end_dt.isoformat(),
            "startTime": start_dt.strftime("%H:%M"),
            "endTime": end_dt.strftime("%H:%M"),
            "startText": f"{m.group('d1')} {m.group('m1')} {m.group('t1')}",
            "endText": f"{m.group('d2')} {m.group('m2')} {m.group('t2')}",
        })

    result = intervals[:2]
    cache[cache_key] = result
    return result


def pick_current(intervals: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """
    Pick active interval for current time (MSK).
    If not inside any, choose the closest future or last.
    """
    # Convert to datetime
    parsed = []
    for it in intervals:
        s = datetime.fromisoformat(it["startIso"])
        e = datetime.fromisoformat(it["endIso"])
        parsed.append((s, e, it))

    # inside interval
    for s, e, it in parsed:
        if s <= now < e:
            return it

    # if before first -> first
    if now < parsed[0][0]:
        return parsed[0][2]

    # otherwise -> last
    return parsed[-1][2]


def build_payload(d: date) -> Dict[str, Any]:
    intervals = extract_intervals(d)
    now_msk = datetime.now(MSK)
    current = pick_current(intervals, now_msk)

    # next switch = end of current interval (if current end is in the future)
    current_end = datetime.fromisoformat(current["endIso"])
    next_switch = current_end if current_end > now_msk else None

    # “удобные строки” как ты любишь
    lines = []
    for it in intervals:
        if it["zodiac"]:
            lines.append(f"{it['day']} лунный день {it['zodiac']} {it['startText']} — {it['endText']}")
        else:
            lines.append(f"{it['day']} лунный день {it['startText']} — {it['endText']}")

    return {
        "date": d.isoformat(),
        "tz": TZ_NAME,
        "nowIso": now_msk.isoformat(),
        "lines": lines,
        "intervals": intervals,
        "current": current,
        "nextSwitchAtIso": next_switch.isoformat() if next_switch else None,
        "nextSwitchInSeconds": int((next_switch - now_msk).total_seconds()) if next_switch else None,
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
    return build_payload(d)


@app.get("/lunar-string")
def lunar_string(
    d: date = Query(..., description="Date in YYYY-MM-DD"),
):
    payload = build_payload(d)
    return "\n".join(payload["lines"])


@app.get("/lunar-now")
def lunar_now(
    d: date = Query(..., description="Date in YYYY-MM-DD"),
):
    """
    Optimized for UI auto-fill:
    - current.startTime
    - current.endTime
    - current.day
    - current.zodiac
    - nextSwitchAtIso
    - nextSwitchInSeconds
    """
    payload = build_payload(d)
    return {
        "date": payload["date"],
        "tz": payload["tz"],
        "nowIso": payload["nowIso"],
        "current": payload["current"],
        "nextSwitchAtIso": payload["nextSwitchAtIso"],
        "nextSwitchInSeconds": payload["nextSwitchInSeconds"],
        "intervals": payload["intervals"],
        "lines": payload["lines"],
    }


@app.get("/debug-raw")
def debug_raw(
    d: date = Query(..., description="Date in YYYY-MM-DD"),
    n: int = Query(2000, description="How many chars to return"),
):
    txt = fetch_page_text(d)
    return {"date": d.isoformat(), "len": len(txt), "sample": txt[:n]}
