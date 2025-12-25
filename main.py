import os
import re
import logging
from datetime import date
from typing import Dict, Any, List

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
    version="2.0.0"
)

# ----------------------------
# PARSING
# ----------------------------

# Пример строки в тексте:
# "6 лунный день 24 декабря 11:35 — 25 декабря 11:42"
# "7 лунный день Рыбы 25 декабря 11:42 — 26 декабря 11:49"
#
# Нам нужно достать 1-2 таких интервала для выбранной даты.
#
# Пояснения:
# - после "лунный день" может быть знак зодиака (одно слово) или ничего
# - разделитель бывает "—" (длинное тире) или "-"
#
RE_INTERVAL = re.compile(
    r"(?P<day>\d{1,2})\s+лунный\s+день"
    r"(?:\s+(?P<zodiac>[А-Яа-яЁё]+))?"
    r"\s+(?P<d1>\d{1,2})\s+(?P<m1>[А-Яа-яЁё]+)\s+(?P<t1>\d{1,2}:\d{2})"
    r"\s*[—-]\s*"
    r"(?P<d2>\d{1,2})\s+(?P<m2>[А-Яа-яЁё]+)\s+(?P<t2>\d{1,2}:\d{2})",
    re.IGNORECASE
)

def fetch_page_text(d: date) -> str:
    """Fetch Rambler page HTML and convert to plain text."""
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

    # Strip scripts/styles/tags
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    logger.info("Text sample for %s: %s", date_str, text[:400])
    return text


def extract_intervals(d: date) -> List[Dict[str, Any]]:
    """
    Возвращает список интервалов на странице даты (обычно 2 штуки):
    [
      {"day": 6, "zodiac": None, "from": "24 декабря 11:35", "to": "25 декабря 11:42"},
      {"day": 7, "zodiac": "Рыбы", "from": "25 декабря 11:42", "to": "26 декабря 11:49"},
    ]
    """
    date_str = d.isoformat()
    cache_key = ("intervals", date_str)
    if cache_key in cache:
        return cache[cache_key]

    text = fetch_page_text(d)

    matches = list(RE_INTERVAL.finditer(text))
    if not matches:
        logger.warning("Could not find lunar intervals for %s. Text excerpt: %s", date_str, text[:1000])
        raise HTTPException(status_code=502, detail="Could not parse Rambler page (blocked or markup changed)")

    intervals: List[Dict[str, Any]] = []
    for m in matches[:4]:  # на всякий случай ограничим
        day = int(m.group("day"))
        zodiac = m.group("zodiac")
        if zodiac:
            zodiac = zodiac.strip()
        from_str = f"{m.group('d1')} {m.group('m1')} {m.group('t1')}"
        to_str = f"{m.group('d2')} {m.group('m2')} {m.group('t2')}"
        intervals.append({
            "day": day,
            "zodiac": zodiac,
            "from": from_str,
            "to": to_str,
        })

    # Обычно на странице есть ровно те интервалы, которые нужны для выбранной даты,
    # поэтому возвращаем первые два.
    result = intervals[:2]
    cache[cache_key] = result
    return result


def build_text(d: date) -> Dict[str, Any]:
    intervals = extract_intervals(d)

    lines = []
    for it in intervals:
        if it["zodiac"]:
            lines.append(f"{it['day']} лунный день {it['zodiac']} {it['from']} — {it['to']}")
        else:
            lines.append(f"{it['day']} лунный день {it['from']} — {it['to']}")

    text = "\n".join(lines)

    return {
        "date": d.isoformat(),
        "lines": lines,
        "text": text,
        "intervals": intervals,
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
    return build_text(d)


@app.get("/lunar-string")
def lunar_string(
    d: date = Query(..., description="Date in YYYY-MM-DD"),
):
    return build_text(d)["text"]


@app.get("/debug-raw")
def debug_raw(
    d: date = Query(..., description="Date in YYYY-MM-DD"),
    n: int = Query(2000, description="How many chars to return"),
):
    txt = fetch_page_text(d)
    return {
        "date": d.isoformat(),
        "len": len(txt),
        "sample": txt[:n],
    }
