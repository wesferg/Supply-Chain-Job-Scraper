"""A polite HTTP session: retries, timeouts, and a small per-host delay."""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (compatible; job-scout/1.0; personal job search; "
    "+https://github.com/)"
)

_last_hit: dict[str, float] = {}
MIN_DELAY = 0.6  # seconds between requests to the same host
# Public job-board APIs built for this kind of traffic can go a bit faster.
HOST_DELAY = {
    "boards-api.greenhouse.io": 0.25,
    "api.lever.co": 0.25,
    "api.ashbyhq.com": 0.25,
    "api.smartrecruiters.com": 0.3,
}


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "en-US",
    })
    return s


SESSION = _session()


_lock = threading.Lock()


def _throttle(url: str) -> None:
    """Reserve the next slot for this host, then sleep until it arrives."""
    host = urlparse(url).netloc
    with _lock:
        now = time.monotonic()
        slot = max(now, _last_hit.get(host, 0.0) + HOST_DELAY.get(host, MIN_DELAY))
        _last_hit[host] = slot
    if slot > now:
        time.sleep(slot - now)


def get(url: str, **kw) -> requests.Response:
    _throttle(url)
    kw.setdefault("timeout", 30)
    return SESSION.get(url, **kw)


def post(url: str, **kw) -> requests.Response:
    _throttle(url)
    kw.setdefault("timeout", 30)
    return SESSION.post(url, **kw)


def get_json(url: str, **kw):
    """GET and parse JSON; returns None on any non-200 or bad JSON."""
    try:
        r = get(url, **kw)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def post_json(url: str, payload: dict, **kw):
    try:
        r = post(url, json=payload, **kw)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None
