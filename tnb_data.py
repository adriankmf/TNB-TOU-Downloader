"""
tnb_data.py
-----------
Pure data logic for the Smart Living /timeseries API -- no browser, no files.

  * build_timeseries_path()  -> the relative URL fetched from inside the Smart Living tab
  * parse_timeseries()       -> API JSON  -> list of usage rows
  * plan_chunks()            -> split a date range into <=30-day windows
  * fetch_days()             -> fetch a range through any fetch function, checking every day
                                has 48 half-hour points and shrinking the window on a short reply

Confirmed live (2026-10-28): 1, 7 and 30-day windows come back complete, but a 77-day window
silently returns only ONE day. So windows are capped at 30 days and every reply is verified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterable
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

TIMESERIES_PATH = "/my_energy_request/timeseries"
POINTS_PER_DAY = 48
MAX_CHUNK_DAYS = 30
# TNB answers HTTP 500 for some old days (seen live on 11 Feb 2025). A bad day is narrowed down
# by splitting the window; after this many server errors in one range the server is treated as down.
MAX_SERVER_ERRORS = 12
API_DATETIME_FORMAT = "%Y-%m-%d %H:%M"

COL_TIMESTAMP = "Timestamp"
COL_KWH = "kWh"
COL_RATEPLAN = "RatePlan"
COL_CATEGORY = "Category"
COLUMNS = (COL_TIMESTAMP, COL_KWH, COL_RATEPLAN, COL_CATEGORY)


class ApiError(RuntimeError):
    """The portal answered, but not with usable timeseries JSON."""


class ServerError(ApiError):
    """The portal crashed on this date range (HTTP 5xx). Other ranges may still work."""


@dataclass(frozen=True)
class UsageRow:
    timestamp: datetime
    kwh: float
    rateplan: str
    category: str


@dataclass
class FetchResult:
    rows: list[UsageRow] = field(default_factory=list)
    # Days that came back with fewer than 48 points even when asked for on their own.
    incomplete_days: list[date] = field(default_factory=list)
    calls: int = 0
    server_errors: int = 0
    stopped: str = ""  # why fetching gave up early ("" = finished); rows so far are still kept
    error: Exception | None = None  # the error that stopped it, for the caller to report


# -- URL ----------------------------------------------------------------------------------------

def build_timeseries_path(sdpudcid: str, start: date, end: date) -> str:
    """
    Relative URL for [start, end) at 30-min granularity. The portal expects a literal '+'
    between date and time (exactly what its own page sends), so '+' is kept unescaped.
    """
    params = {
        "orgname": "TNB",
        "sdpudcid": sdpudcid,
        "dateformat": "yyyy-MM-dd+HH:mm",
        "timezone": "Asia/Kuala_Lumpur",
        "view": "DAY",
        "granularity": "MIN30",
        "start": f"{start:%Y-%m-%d}+00:00",
        "end": f"{end:%Y-%m-%d}+00:00",
        "metric": "usage",
    }
    return f"{TIMESERIES_PATH}?{urlencode(params, safe='+:/')}"


# -- Parsing ------------------------------------------------------------------------------------

def parse_timeseries(body: dict) -> list[UsageRow]:
    """Turn the API JSON into UsageRows. Raises ApiError when the reply carries errors."""
    if not isinstance(body, dict):
        raise ApiError(f"Unexpected reply type: {type(body).__name__}")
    if body.get("errorMessage") or body.get("errors"):
        raise ApiError(f"Portal error: {body.get('errorMessage') or body.get('errors')}")

    data = body.get("data")
    if not isinstance(data, dict) or "timeseries" not in data:
        raise ApiError(f"Reply has no data.timeseries: {str(body)[:200]}")

    rows: dict[datetime, UsageRow] = {}
    for slot in data.get("timeseries") or []:
        for point in slot.get("data") or []:
            row = _parse_point(point)
            if row is not None:
                rows[row.timestamp] = row
    return sorted(rows.values(), key=lambda r: r.timestamp)


def _parse_point(point: dict) -> UsageRow | None:
    # TNB has sent both "usage" and "USAGE" (seen live 2026-10-29).
    if str(point.get("metric") or "usage").lower() != "usage":
        return None
    raw_ts, raw_value = point.get("datetime"), point.get("value")
    if raw_ts is None or raw_value is None:
        return None
    try:
        ts = datetime.strptime(str(raw_ts).replace("+", " "), API_DATETIME_FORMAT)
        kwh = float(raw_value)
    except (TypeError, ValueError):
        logger.warning("Skipping unreadable point: %s", point)
        return None
    return UsageRow(ts, kwh, str(point.get("rateplan") or ""), str(point.get("category") or ""))


# -- Chunking -----------------------------------------------------------------------------------

def days_between(start: date, end: date) -> list[date]:
    """Every day in [start, end)."""
    return [start + timedelta(days=i) for i in range((end - start).days)]


def plan_chunks(start: date, end: date, max_days: int = MAX_CHUNK_DAYS) -> list[tuple[date, date]]:
    """Split [start, end) into consecutive windows of at most max_days."""
    if max_days < 1:
        raise ValueError("max_days must be >= 1")
    chunks, cursor = [], start
    while cursor < end:
        stop = min(cursor + timedelta(days=max_days), end)
        chunks.append((cursor, stop))
        cursor = stop
    return chunks


def contiguous_runs(days: Iterable[date]) -> list[tuple[date, date]]:
    """Group days into [start, end) runs of consecutive dates."""
    runs: list[tuple[date, date]] = []
    for day in sorted(set(days)):
        if runs and runs[-1][1] == day:
            runs[-1] = (runs[-1][0], day + timedelta(days=1))
        else:
            runs.append((day, day + timedelta(days=1)))
    return runs


def count_per_day(rows: Iterable[UsageRow]) -> dict[date, int]:
    counts: dict[date, int] = {}
    for row in rows:
        counts[row.timestamp.date()] = counts.get(row.timestamp.date(), 0) + 1
    return counts


# -- Verified fetching --------------------------------------------------------------------------

FetchFn = Callable[[date, date], list[UsageRow]]
ProgressFn = Callable[[date, date], None]


def fetch_days(fetch: FetchFn, start: date, end: date, *,
               max_days: int = MAX_CHUNK_DAYS,
               on_window: ProgressFn | None = None) -> FetchResult:
    """
    Fetch [start, end) in <=max_days windows and verify each day has 48 points.

    A window that comes back short (some days missing or partial) is re-requested for just
    the missing days, in windows half the size, down to single days. A window that comes back
    completely EMPTY is not split further: that is "no data at TNB" (before the meter's first
    reading, or not published yet), and splitting it would only burn calls.

    A window the server crashes on (ServerError) is split the same way, so one bad day is
    reported as incomplete instead of failing the whole sync. Too many crashes -> stop early,
    keep what was fetched and say why in result.stopped.

    Any other error from fetch() also stops fetching; it is returned in result.error (not
    raised) so the rows that already arrived are not lost. The caller must check it.
    """
    result = FetchResult()
    collected: dict[datetime, UsageRow] = {}
    try:
        for window_start, window_end in plan_chunks(start, end, max_days):
            _fetch_window(fetch, window_start, window_end, collected, result, on_window)
    except _TooManyServerErrors as exc:
        result.stopped = str(exc)
    except Exception as exc:  # noqa: BLE001 -- handed back to the caller in result.error
        result.stopped, result.error = str(exc), exc
    result.rows = sorted(collected.values(), key=lambda r: r.timestamp)
    result.incomplete_days.sort()
    return result


class _TooManyServerErrors(Exception):
    pass


def _fetch_window(fetch: FetchFn, start: date, end: date, collected: dict,
                  result: FetchResult, on_window: ProgressFn | None) -> None:
    if on_window:
        on_window(start, end)
    result.calls += 1
    try:
        rows = [r for r in fetch(start, end) if start <= r.timestamp.date() < end]
    except ServerError as exc:
        _on_server_error(fetch, start, end, collected, result, on_window, exc)
        return
    for row in rows:
        collected[row.timestamp] = row

    counts = count_per_day(collected[ts] for ts in collected if start <= ts.date() < end)
    short_days = [d for d in days_between(start, end) if counts.get(d, 0) < POINTS_PER_DAY]
    if not short_days:
        return

    span = (end - start).days
    if span == 1 or not rows:
        result.incomplete_days.extend(short_days)
        return

    logger.info("Window %s..%s came back short (%d of %d days complete) -- retrying smaller.",
                start, end - timedelta(days=1), span - len(short_days), span)
    smaller = max(1, span // 2)
    for run_start, run_end in contiguous_runs(short_days):
        for sub_start, sub_end in plan_chunks(run_start, run_end, smaller):
            _fetch_window(fetch, sub_start, sub_end, collected, result, on_window)


def _on_server_error(fetch: FetchFn, start: date, end: date, collected: dict,
                     result: FetchResult, on_window: ProgressFn | None, exc: ServerError) -> None:
    result.server_errors += 1
    if result.server_errors >= MAX_SERVER_ERRORS:
        raise _TooManyServerErrors(f"TNB server kept failing ({exc}); stopped early.") from exc
    span = (end - start).days
    if span == 1:
        logger.info("TNB server error for %s -- marked incomplete.", start)
        result.incomplete_days.append(start)
        return
    half = start + timedelta(days=span // 2)
    for sub_start, sub_end in ((start, half), (half, end)):
        _fetch_window(fetch, sub_start, sub_end, collected, result, on_window)


def split_unavailable_tail(incomplete: list[date], rows: list[UsageRow]) -> tuple[list[date], list[date]]:
    """
    Split incomplete days into (gaps, not_yet_available). Days after the last day that has any
    data are simply not published yet; incomplete days before it are real gaps.
    """
    if not rows:
        return [], sorted(incomplete)
    last_day = max(r.timestamp for r in rows).date()
    gaps = sorted(d for d in incomplete if d < last_day)
    tail = sorted(d for d in incomplete if d >= last_day)
    # The last day itself counts as a gap only if later days exist, which they don't by definition;
    # a partial last day is just "today/yesterday still filling in".
    return gaps, tail
