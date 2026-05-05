"""
tnb_api.py
----------
Fetches 30-minute interval usage data from the myTNB /timeseries endpoint.

CONFIRMED from live API response (April 2026)
---------------------------------------------
Endpoint  : GET {BASE_URL}/my_energy_request/timeseries
Auth      : Session cookie (via tnb_auth.get_session())
Portal URL: https://smartliving.myaccount.mytnb.com.my/commodity/electric/usage

Query parameters (all confirmed):
  orgname     = TNB
  sdpudcid    = <your meter ID, e.g. 40000000000013274078>
  dateformat  = yyyy-MM-dd HH:mm
  timezone    = Asia/Kuala_Lumpur
  view        = DAY
  granularity = MIN30
  start       = 2026-04-11 00:00
  end         = 2026-04-12 00:00

Response structure (confirmed):
  {
    "data": {
      "timeseries": [
        {
          "start": "2026-04-11 00:00",
          "end":   "2026-04-11 00:30",
          "data": [
            {
              "datetime":  "2026-04-11 00:00",
              "value":     0.57,
              "rateplan":  "TNB NEM Domestic General",
              ...
            }
          ]
        }, ...
      ]
    }
  }
"""

import logging
from datetime import datetime, timedelta
from typing import List
from urllib.parse import urlencode

import pandas as pd

import config
from tnb_auth import get_session, invalidate_session

logger = logging.getLogger(__name__)

# ── Column names ──────────────────────────────────────────────────────────────
COL_TIMESTAMP = "Timestamp"
COL_KWH       = "kWh"
COL_RATEPLAN  = "RatePlan"

# ── API constants ─────────────────────────────────────────────────────────────
USAGE_PAGE_PATH = "/commodity/electric/usage"
# Confirmed from e3LoginReturn cookie — the URL the browser was trying to
# reach when the session expired.  /timeseries gives "TimeseriesController
# not found" (wrong PHP routing); this path hits the right controller.
TIMESERIES_PATH = "/my_energy_request/timeseries"
MAX_LOGIN_RETRIES = 1


# ── Public interface ──────────────────────────────────────────────────────────

def get_usage_dataframe(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    """
    Fetch all 30-min interval records between *start_dt* and *end_dt*.
    Loops day-by-day (the API returns one calendar day per request).
    """
    # Warm up the session — the /timeseries endpoint enforces its own session
    # check and returns {"redirect": true} unless the usage page was visited first.
    _warm_up_session()

    all_frames: List[pd.DataFrame] = []
    current_day = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day     = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    total_days = max(1, (end_day - current_day).days + 1)
    logger.info(
        "Fetching %d day(s) of data: %s -> %s",
        total_days,
        start_dt.strftime("%Y-%m-%d"),
        end_dt.strftime("%Y-%m-%d"),
    )

    redirect_count = 0
    checked_days   = 0

    while current_day <= end_day:
        next_day = current_day + timedelta(days=1)
        day_df, was_redirect = _fetch_day(current_day, next_day)
        checked_days += 1

        if was_redirect:
            redirect_count += 1
            # Fast-fail after 5 consecutive redirects — almost certainly a dead session.
            if redirect_count >= 5:
                raise ValueError(
                    "Session cookie has expired.\n\n"
                    "The portal is redirecting every request to the login page.\n\n"
                    "Please refresh your cookie:\n"
                    "  1. Open Chrome → https://smartliving.myaccount.mytnb.com.my\n"
                    "  2. Log in and navigate to the Usage page\n"
                    "  3. Press F12 → Network → click any request → Headers\n"
                    "  4. Copy the full 'Cookie:' value\n"
                    "  5. Paste it into the Settings tab and click Save Settings"
                )
        else:
            redirect_count = 0  # reset streak on a successful response

        if not day_df.empty:
            mask = (day_df[COL_TIMESTAMP] >= start_dt) & (day_df[COL_TIMESTAMP] <= end_dt)
            day_df = day_df[mask]

        if not day_df.empty:
            all_frames.append(day_df)

        current_day = next_day

    if not all_frames:
        logger.info("No data returned by the API for the requested window.")
        return pd.DataFrame(columns=[COL_TIMESTAMP, COL_KWH, COL_RATEPLAN])

    result = pd.concat(all_frames, ignore_index=True)
    result = result.sort_values(COL_TIMESTAMP).reset_index(drop=True)
    logger.info("Total records fetched: %d", len(result))
    return result


# ── Private helpers ───────────────────────────────────────────────────────────

def _warm_up_session() -> None:
    """
    Visit the main usage page so the portal sets all page-specific cookies
    before we hit the /timeseries endpoint.  Without this the server returns
    an HTML page instead of JSON.
    """
    usage_url = config.TNB_API_BASE_URL.rstrip("/") + USAGE_PAGE_PATH
    session = get_session()

    logger.info("Warming up session — visiting usage page…")
    resp = session.get(
        usage_url,
        timeout=config.REQUEST_TIMEOUT,
        headers={"Accept": "text/html,application/xhtml+xml,*/*"},
    )
    logger.debug(
        "Warm-up response: HTTP %s, URL: %s, cookies: %s",
        resp.status_code,
        resp.url,
        list(session.cookies.keys()),
    )


def _fetch_day(day_start: datetime, day_end: datetime):
    """
    Make one GET /timeseries request for a single calendar day.
    Returns (DataFrame, was_redirect: bool).
    was_redirect=True means the server issued a 302/soft-redirect (likely expired session).
    Retries once on soft-redirect.
    """
    url    = _build_url(day_start, day_end)
    _empty = pd.DataFrame(columns=[COL_TIMESTAMP, COL_KWH, COL_RATEPLAN])
    logger.debug("GET %s", url)

    for attempt in range(MAX_LOGIN_RETRIES + 1):
        session = get_session()
        response = session.get(
            url,
            timeout=config.REQUEST_TIMEOUT,
            allow_redirects=False,   # don't follow — inspect the redirect manually
            headers={
                "Accept": "application/json, text/javascript, */*",
                "Referer": config.TNB_API_BASE_URL.rstrip("/") + USAGE_PAGE_PATH,
                "X-Requested-With": "XMLHttpRequest",
            },
        )

        logger.debug(
            "Response: HTTP %s | URL: %s | Location: %s",
            response.status_code,
            response.url,
            response.headers.get("Location", "(none)"),
        )

        # ── 302 / 3xx redirect ──────────────────────────────────────────────
        # The API can issue a 302 that strips all query params (bare endpoint URL →
        # 500 "view not found") or redirects to /login (session expired).
        # Signal the caller with was_redirect=True so it can detect pattern of
        # repeated redirects that indicate a dead session.
        if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
            loc = response.headers.get("Location", "")
            logger.debug(
                "HTTP %s redirect for %s → %s",
                response.status_code, day_start.strftime("%Y-%m-%d"), loc,
            )
            return _empty, True

        _raise_for_status_with_detail(response)

        # ── Soft redirect ───────────────────────────────────────────────────
        # API returns {"redirect": true, "redirectTo": "/login"} as 200 JSON.
        # Retry once after revisiting the usage page.
        try:
            body_check = response.json()
            if body_check.get("redirect"):
                if attempt < MAX_LOGIN_RETRIES:
                    logger.warning(
                        "Soft-redirect to '%s' for %s — warming up and retrying…",
                        body_check.get("redirectTo", "?"),
                        day_start.strftime("%Y-%m-%d"),
                    )
                    _warm_up_session()
                    continue
                else:
                    logger.debug(
                        "Soft-redirect persists for %s after warm-up — skipping day.",
                        day_start.strftime("%Y-%m-%d"),
                    )
                    return _empty, True
        except Exception:
            pass  # Not JSON — fall through to normal parsing

        break

    # Log raw response for debugging if it's not JSON.
    raw = response.text
    try:
        body = response.json()
    except Exception:
        logger.error(
            "Non-JSON response from /timeseries for %s.\n"
            "Status: %s | URL: %s\nBody (first 500 chars):\n%s",
            day_start.strftime("%Y-%m-%d"),
            response.status_code,
            response.url,
            raw[:500],
        )
        raise

    _check_api_errors(body, url)
    return _flatten_response(body, day_start), False


def _build_url(day_start: datetime, day_end: datetime) -> str:
    """Construct the full /timeseries URL for a single day (confirmed params)."""
    # Parameters confirmed from e3LoginReturn cookie URL (decoded).
    # IMPORTANT: The browser sends datetimes with a LITERAL '+' between date
    # and time (e.g. "2026-04-11+00:00"), URL-encoded as %2B.  Using a space
    # (encoded as '+') causes the PHP framework to misparse the query string
    # and return "Undefined index: view".  We build the datetime string with
    # a literal '+' so that urlencode() encodes it as '%2B', matching the
    # browser exactly.
    def _fmt(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d") + "+" + dt.strftime("%H:%M")

    params = {
        "orgname":     config.TNB_ORG_NAME,
        "sdpudcid":    config.TNB_SDPUDCID,
        "dateformat":  "yyyy-MM-dd+HH:mm",   # literal '+', not space
        "timezone":    config.TNB_TIMEZONE,
        "view":        "DAY",
        "granularity": config.TNB_GRANULARITY,
        "start":       _fmt(day_start),
        "end":         _fmt(day_end),
        "metric":      "usage",
    }
    base = config.TNB_API_BASE_URL.rstrip("/") + TIMESERIES_PATH
    return f"{base}?{urlencode(params)}"


def _flatten_response(body: dict, day: datetime) -> pd.DataFrame:
    """Parse confirmed JSON structure into a tidy DataFrame."""
    _empty = pd.DataFrame(columns=[COL_TIMESTAMP, COL_KWH, COL_RATEPLAN])

    try:
        timeseries = body["data"]["timeseries"]
    except (KeyError, TypeError):
        logger.debug(
            "No 'data.timeseries' in response for %s — body: %s",
            day.strftime("%Y-%m-%d"),
            str(body)[:200],
        )
        return _empty

    if not timeseries:
        logger.debug("No timeseries slots returned for %s.", day.strftime("%Y-%m-%d"))
        return _empty

    rows = []
    for slot in timeseries:
        slot_data = slot.get("data", [])
        if not slot_data:
            continue
        entry    = slot_data[0]
        ts_str   = entry.get("datetime")
        kwh_val  = entry.get("value")
        rateplan = entry.get("rateplan", "")

        if ts_str is None or kwh_val is None:
            continue

        rows.append({COL_TIMESTAMP: ts_str, COL_KWH: kwh_val, COL_RATEPLAN: rateplan})

    if not rows:
        return _empty

    df = pd.DataFrame(rows)
    df[COL_TIMESTAMP] = pd.to_datetime(df[COL_TIMESTAMP], format=config.TNB_DATEFMT_PY)
    df[COL_KWH]       = pd.to_numeric(df[COL_KWH], errors="coerce")
    return df.sort_values(COL_TIMESTAMP).reset_index(drop=True)


def _check_api_errors(body: dict, url: str) -> None:
    error_msg = body.get("errorMessage", "")
    errors    = body.get("errors", [])
    if error_msg or errors:
        raise ValueError(
            f"API returned errors for {url}.\nerrorMessage: {error_msg}\nerrors: {errors}"
        )


def _raise_for_status_with_detail(response) -> None:
    try:
        response.raise_for_status()
    except Exception:
        logger.error(
            "HTTP %s from %s — body: %s",
            response.status_code, response.url, response.text[:400],
        )
        raise
