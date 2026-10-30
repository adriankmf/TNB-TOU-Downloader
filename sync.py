"""
sync.py
-------
Ties the browser (tnb_portal), the data checks (tnb_data) and the workbook (excel_store) together.
Used by both the app window (app.py) and the command line (main.py).

Every function takes the settings dict and returns an UPDATED COPY (new account cache etc.) --
the caller decides when to save it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import config
import excel_store
from tnb_data import (ApiError, FetchResult, contiguous_runs, fetch_days, plan_chunks,
                      plan_chunks_newest_first, split_unavailable_tail)
from tnb_portal import PortalError, SessionExpired, TnbPortal, TnbThrottled

logger = logging.getLogger(__name__)

Log = Callable[[str], None]
Progress = Callable[[float], None]

# Be gentle with TNB: repeated logins look like an attack and get the account throttled.
# Waiting in place costs no login; a full re-login is allowed only MAX_RELOGINS times per sync.
MAX_SESSION_RECOVERIES = 4
MAX_RELOGINS = 1


@dataclass
class SyncResult:
    ca_no: str
    file: str
    rows_added: int = 0
    latest: str | None = None              # last Timestamp in the file after the sync
    gaps: list[str] = field(default_factory=list)          # incomplete days inside the history
    pending: list[str] = field(default_factory=list)       # newest days TNB has not published yet
    unavailable: list[str] = field(default_factory=list)   # days TNB returned incomplete this run
    stopped: str = ""                      # why downloading stopped early ("" = finished)
    calls: int = 0
    login: str = ""                        # "session" or "login"
    first: str | None = None               # first Timestamp in the file after the sync
    more: bool = False                     # another batch is needed (history or new days left)
    throttled: bool = False                # TNB stopped handing out data this batch


def _noop(*_args) -> None:
    pass


def make_portal(settings: dict, log: Log) -> TnbPortal:
    return TnbPortal(config.BROWSER_PROFILE_DIR, settings.get("browser", "chrome"), log=log)


# -- Migration ----------------------------------------------------------------------------------

def migrate_if_needed(settings: dict, log: Log = _noop) -> dict:
    """Move the old single tnb_usage.xlsx into the selected account's file (once)."""
    legacy = settings.get("legacy_excel")
    ca_no = settings.get("selected_account")
    if not legacy or not ca_no:
        return settings  # no account known yet -- tried again after the first sign-in
    new_path = excel_store.migrate_legacy(Path(legacy), Path(settings["excel_folder"]), ca_no)
    if new_path:
        log(f"Moved your old Excel history to {new_path.name} (backup kept next to it).")
    return {k: v for k, v in settings.items() if k != "legacy_excel"}


# -- Accounts -----------------------------------------------------------------------------------

def refresh_accounts(settings: dict, password: str | None, *, log: Log = _noop,
                     portal_factory=make_portal) -> tuple[list[dict], dict, str]:
    """Log in, read the account list, cache it. Returns (accounts, new_settings, login_mode)."""
    with portal_factory(settings, log) as portal:
        mode = portal.ensure_login(settings.get("email", ""), password)
        accounts = portal.list_accounts()
    for acc in accounts:
        settings = config.with_account(settings, acc["ca_no"], name=acc["name"], guid=acc["guid"])
    if accounts and settings.get("selected_account") not in {a["ca_no"] for a in accounts}:
        settings = {**settings, "selected_account": accounts[0]["ca_no"]}
    return accounts, settings, mode


# -- Sync ---------------------------------------------------------------------------------------
#
# Downloads run in BATCHES so TNB never sees a burst: one batch is at most BATCH_DAYS of data
# (BATCH_DAYS / 30 requests, MIN_FETCH_GAP_S apart), then download_all() pauses BATCH_PAUSE_S
# with the browser left open (no new login) before the next batch. If TNB still starts limiting
# the account, it waits THROTTLE_PAUSE_S and carries on, giving up after MAX_THROTTLE_PAUSES.
#
# Order inside a batch: new days first (oldest-first from the last saved reading, so the file
# never gets a hole), then gaps, then older history walking BACK from the first saved day.

BATCH_DAYS = 180
BATCH_PAUSE_S = 3 * 60
THROTTLE_PAUSE_S = 35 * 60
MAX_THROTTLE_PAUSES = 2
RECENT_DAYS = 30  # a new account gets its last 30 days first, then history from the oldest end

Window = tuple[date, date]


def sync_range(path: Path, bootstrap_start: str, today: date) -> tuple[date, date] | None:
    """[start, end) of NEW days still to download (after the last saved reading). None when up to date."""
    latest = excel_store.watermark(path)
    if latest is None:
        start = max(history_target(bootstrap_start, None, today), today + timedelta(days=1 - RECENT_DAYS))
    elif latest.hour == 23 and latest.minute == 30:
        start = latest.date() + timedelta(days=1)
    else:
        start = latest.date()  # last day only partly saved -- fetch it again, duplicates are skipped
    end = today + timedelta(days=1)
    return (start, end) if start < end else None


def history_target(bootstrap_start: str, floor: str | None, today: date) -> date:
    """Oldest day to download: the user's date, but never before the meter's first data."""
    target = datetime.strptime(bootstrap_start or config.default_bootstrap_start(today), "%Y-%m-%d").date()
    return max(target, date.fromisoformat(floor)) if floor else target


def plan_windows(path: Path, settings: dict, ca_no: str, today: date, *,
                 fill_gaps: bool = False) -> tuple[list[Window], list[Window]]:
    """(new + gap windows oldest-first, history windows newest-first), each at most 30 days."""
    forward: list[Window] = []
    new_days = sync_range(path, settings.get("bootstrap_start", ""), today)
    if new_days:
        forward += plan_chunks(*new_days)
    if fill_gaps:
        for run in contiguous_runs(excel_store.find_gaps(excel_store.read_frame(path))):
            forward += plan_chunks(*run)

    floor = settings.get("accounts", {}).get(ca_no, {}).get("history_floor")
    target = history_target(settings.get("bootstrap_start", ""), floor, today)
    # History ends where the saved data starts (or, for a new account, where its recent days start).
    history_end = excel_store.first_day(path) or (new_days[0] if new_days else None)
    backward = plan_chunks_newest_first(target, history_end) if history_end and target < history_end else []
    return forward, backward


def _take_batch(forward: list[Window], backward: list[Window],
                budget_days: int) -> tuple[list[tuple[Window, bool]], bool]:
    """Windows for one batch as (window, is_history), and whether more is left for later."""
    batch, used = [], 0
    for window, is_history in [(w, False) for w in forward] + [(w, True) for w in backward]:
        days = (window[1] - window[0]).days
        if batch and used + days > budget_days:
            return batch, True
        batch.append((window, is_history))
        used += days
    return batch, False


def sync_account(settings: dict, ca_no: str, password: str | None, *, fill_gaps: bool = False,
                 folder: str | None = None, today: date | None = None, log: Log = _noop,
                 progress: Progress = _noop, portal_factory=make_portal,
                 budget_days: int | None = None,
                 should_stop: Callable[[], bool] = lambda: False) -> tuple[SyncResult, dict]:
    """Download ONE batch (at most budget_days) for one account into its Excel file.
    result.more says whether another batch is needed; download_all() runs them all."""
    today = today or date.today()
    path = excel_store.account_file(folder or settings["excel_folder"], ca_no)
    result = SyncResult(ca_no=ca_no, file=str(path))

    forward, backward = plan_windows(path, settings, ca_no, today, fill_gaps=fill_gaps)
    batch, result.more = _take_batch(forward, backward, budget_days or BATCH_DAYS)
    if not batch:
        log("Already up to date.")
        return _finish(result, path), settings

    total_days = sum((e - s).days for (s, e), _ in batch) or 1
    log(f"This batch: {total_days} day(s) for {ca_no}" + (" (more batches to follow)" if result.more else "") + ".")

    fetched, meter_start = FetchResult(), None
    try:
        with portal_factory(settings, log) as portal:
            result.login = portal.ensure_login(settings.get("email", ""), password)
            account = dict(settings.get("accounts", {}).get(ca_no, {}), ca_no=ca_no)
            if not account.get("guid") and not account.get("smartmeter_url"):
                for acc in portal.list_accounts():
                    settings = config.with_account(settings, acc["ca_no"], name=acc["name"], guid=acc["guid"])
                account = dict(settings["accounts"].get(ca_no, {}), ca_no=ca_no)
            info = portal.open_smart_living(account)
            settings = config.with_account(settings, ca_no, **info)
            account.update(info)
            meter_start = _fetch_batch(portal, account, settings, password, batch, fetched, result,
                                       log=log, progress=progress, should_stop=should_stop, total_days=total_days)
    except TnbThrottled as exc:
        # Not an error to raise: download_all() waits and carries on; a single sync just says so.
        result.throttled, result.stopped, result.more = True, str(exc), True
        log(str(exc))
    except Exception:
        # Keep whatever already arrived, then let the caller report the error.
        _save(result, path, fetched, log)
        raise

    _save(result, path, fetched, log)
    if meter_start:
        # Remember where TNB's data begins so later syncs never ask for the empty months again.
        first = excel_store.first_day(path) or meter_start
        settings = config.with_account(settings, ca_no, history_floor=first.isoformat())
    return _finish(result, path), settings


def _fetch_batch(portal, account: dict, settings: dict, password: str | None,
                 batch: list[tuple[Window, bool]], fetched: FetchResult, result: SyncResult, *,
                 log: Log, progress: Progress, should_stop: Callable[[], bool], total_days: int) -> date | None:
    """Fetch the batch's windows into `fetched`. Returns the end of an empty history window
    (TNB has nothing older: the meter's data starts there), else None."""
    fetch = _resilient_fetch(portal, account, settings, password)
    days_done = 0
    for (start, end), is_history in batch:
        if should_stop():
            result.stopped, result.more = "Stopped by you.", True
            return None
        log(f"Fetching {start:%d %b %Y} .. {end - timedelta(days=1):%d %b %Y}")
        part = fetch_days(fetch, start, end)
        fetched.rows += part.rows
        fetched.incomplete_days += part.incomplete_days
        fetched.calls += part.calls
        days_done += (end - start).days
        progress(days_done / total_days)
        if part.error:
            raise part.error
        if part.stopped:
            result.stopped, result.more = part.stopped, True
            log(part.stopped)
            return None
        if is_history and not part.rows:
            log(f"TNB has no readings before {end:%d %b %Y} -- that is the start of this meter's history.")
            result.more = False
            return end
    return None


def download_all(settings: dict, ca_no: str, password: str | None, *, save: Callable[[dict], None] = _noop,
                 log: Log = _noop, status: Log = _noop, progress: Progress = _noop,
                 should_stop: Callable[[], bool] = lambda: False, sleep: Callable[[float], object] = time.sleep,
                 fill_gaps: bool = False, folder: str | None = None, today: date | None = None,
                 portal_factory=make_portal) -> tuple[SyncResult, dict]:
    """
    Run batches until everything is downloaded, pausing between them. One browser for the whole
    run, so a pause costs no login. Settings are handed to save() after every batch, so closing
    the app loses at most the batch in progress.
    """
    shared = _SharedPortal(portal_factory)
    total_rows, throttle_pauses, batch_no = 0, 0, 0
    try:
        while True:
            batch_no += 1
            status(f"Downloading batch {batch_no}...")
            result, settings = sync_account(settings, ca_no, password, fill_gaps=fill_gaps and batch_no == 1,
                                            folder=folder, today=today, log=log, progress=progress,
                                            portal_factory=shared, should_stop=should_stop)
            save(settings)
            total_rows += result.rows_added
            if should_stop() or not result.more:
                break
            if result.stopped:
                throttle_pauses += 1
                if throttle_pauses > MAX_THROTTLE_PAUSES:
                    result.stopped = "TNB keeps limiting this account -- try again in a few hours."
                    log(result.stopped)
                    break
                pause, why = THROTTLE_PAUSE_S, "TNB is limiting requests"
            else:
                throttle_pauses, pause, why = 0, BATCH_PAUSE_S, f"batch {batch_no} saved"
            log(f"{why[0].upper()}{why[1:]} -- pausing {pause // 60} min so TNB does not block the account.")
            if not _pause(pause, why, status, should_stop, sleep):
                break
    finally:
        shared.close()
    if should_stop():
        result.stopped = "Stopped by you."
        log("Stopped. Everything downloaded so far is saved; press Sync to continue.")
    result.rows_added = total_rows
    return result, settings


def _pause(seconds: int, why: str, status: Log, should_stop: Callable[[], bool],
           sleep: Callable[[float], object]) -> bool:
    """Count down, telling the UI every second. False when the user pressed Stop."""
    for left in range(seconds, 0, -1):
        if should_stop():
            return False
        status(f"{why[0].upper()}{why[1:]}. Next batch in {left // 60}:{left % 60:02d}")
        sleep(1)
    return not should_stop()


class _SharedPortal:
    """portal_factory that opens the browser on first use and keeps it open across batches."""

    def __init__(self, factory) -> None:
        self.factory, self._args, self._cm, self._portal = factory, None, None, None

    def __call__(self, settings: dict, log: Log) -> "_SharedPortal":
        self._args = (settings, log)
        return self

    def __enter__(self):
        if self._portal is None:
            self._cm = self.factory(*self._args)
            self._portal = self._cm.__enter__()
        return self._portal

    def __exit__(self, *exc) -> None:
        pass  # kept open for the next batch; close() ends it

    def close(self) -> None:
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
            self._cm = self._portal = None


def _save(result: SyncResult, path: Path, fetched: FetchResult, log: Log) -> None:
    result.calls = fetched.calls
    gaps, pending = split_unavailable_tail(fetched.incomplete_days, fetched.rows)
    result.unavailable = [d.isoformat() for d in gaps]
    result.pending = [d.isoformat() for d in pending]
    result.rows_added = excel_store.append_rows(path, fetched.rows)
    log(f"Saved {result.rows_added} new half-hour reading(s) to {path.name}.")
    if result.unavailable:
        log(f"TNB returned incomplete data for {len(result.unavailable)} day(s): {', '.join(result.unavailable[:10])}")


def _resilient_fetch(portal, account: dict, settings: dict, password: str | None):
    """
    fetch(start, end) that survives the Smart Living session dropping mid-sync: first wait for
    it to come back in place, and only if that fails log in and open Smart Living again.
    At most MAX_SESSION_RECOVERIES per sync (MAX_RELOGINS of them a new login), so a flapping
    session can never turn into a stream of logins.
    """
    left = {"recoveries": MAX_SESSION_RECOVERIES, "relogins": MAX_RELOGINS}

    def wait_in_place() -> None:
        portal.recover_data_session(account["sdpudcid"])

    def reopen() -> None:
        portal.ensure_login(settings.get("email", ""), password)
        account.update(portal.open_smart_living(account))

    def fetch(start: date, end: date):
        for recovery in (wait_in_place, reopen, None):
            try:
                return portal.fetch_rows(account["sdpudcid"], start, end)
            except SessionExpired as exc:
                if recovery is reopen and left["relogins"] <= 0:
                    recovery = None
                if recovery is None or left["recoveries"] <= 0:
                    raise TnbThrottled("Smart Living keeps logging out -- TNB may be limiting "
                                       "logins. Try again in about 30 minutes.") from exc
                left["recoveries"] -= 1
                if recovery is reopen:
                    left["relogins"] -= 1
                logger.info("Smart Living session dropped (%s) -- %s.", exc, recovery.__name__)
                recovery()

    return fetch


def _finish(result: SyncResult, path: Path) -> SyncResult:
    df = excel_store.read_frame(path)
    if not df.empty:
        result.latest = df["Timestamp"].max().strftime("%Y-%m-%d %H:%M")
        result.first = df["Timestamp"].min().strftime("%Y-%m-%d %H:%M")
    result.gaps = [d.isoformat() for d in excel_store.find_gaps(df)]
    return result
