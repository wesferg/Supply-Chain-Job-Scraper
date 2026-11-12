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
    "boards-api.greenhouse.io": 0.4,   # Greenhouse rate-limits bursts from cloud IPs
    "api.lever.co": 0.25,
    "api.ashbyhq.com": 0.25,
    "api.smartrecruiters.com": 0.3,
}


def _session(retries: int = 3) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries,
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
QUICK = _session(retries=0)  # for flaky bulk indexes: fail fast, the caller decides what's next


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


def get(url: str, quick: bool = False, **kw) -> requests.Response:
    _throttle(url)
    kw.setdefault("timeout", 30)
    return (QUICK if quick else SESSION).get(url, **kw)


def post(url: str, **kw) -> requests.Response:
    _throttle(url)
    kw.setdefault("timeout", 30)
    return SESSION.post(url, **kw)


def get_json(url: str, quick: bool = False, **kw):
    """GET and parse JSON; returns None on any non-200 or bad JSON."""
    try:
        r = get(url, quick=quick, **kw)
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


# Status-aware calls, so a check can tell "this board doesn't exist" (404 and friends)
# apart from "couldn't tell right now" (rate limit, timeout, server error).
GONE = (400, 404, 410, 422)


def status_json(url: str, payload: dict | None = None, **kw) -> tuple[int, object]:
    """(status, parsed JSON or None). Status 0 means the request itself failed."""
    try:
        r = post(url, json=payload, **kw) if payload is not None else get(url, **kw)
    except requests.RequestException:
        return 0, None
    if r.status_code != 200:
        return r.status_code, None
    try:
        return 200, r.json()
    except ValueError:
        return 200, None


def check(url: str, shape, payload: dict | None = None, **kw):
    """True if the endpoint answers with the expected shape, False if it's definitely
    not there, None if we couldn't tell (try again another day)."""
    code, data = status_json(url, payload, **kw)
    if code == 200:
        return bool(shape(data))
    if code in GONE:
        return False
    return None  # 0 (network), 403 (blocked), 429 (rate limit), 5xx: unknown
