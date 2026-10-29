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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import config
import excel_store
from tnb_data import ApiError, FetchResult, contiguous_runs, fetch_days, split_unavailable_tail
from tnb_portal import PortalError, SessionExpired, TnbPortal

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

def sync_range(path: Path, bootstrap_start: str, today: date) -> tuple[date, date] | None:
    """[start, end) still to download, from the file's watermark. None when already up to date."""
    latest = excel_store.watermark(path)
    if latest is None:
        start = datetime.strptime(bootstrap_start or config.DEFAULT_BOOTSTRAP_START, "%Y-%m-%d").date()
    elif latest.hour == 23 and latest.minute == 30:
        start = latest.date() + timedelta(days=1)
    else:
        start = latest.date()  # last day only partly saved -- fetch it again, duplicates are skipped
    end = today + timedelta(days=1)
    return (start, end) if start < end else None


def sync_account(settings: dict, ca_no: str, password: str | None, *, fill_gaps: bool = False,
                 folder: str | None = None, today: date | None = None, log: Log = _noop,
                 progress: Progress = _noop, portal_factory=make_portal) -> tuple[SyncResult, dict]:
    """Download everything missing for one account into its Excel file."""
    today = today or date.today()
    path = excel_store.account_file(folder or settings["excel_folder"], ca_no)
    result = SyncResult(ca_no=ca_no, file=str(path))

    # New data first: an old gap TNB can't serve must never block today's readings.
    windows: list[tuple[date, date]] = []
    forward = sync_range(path, settings.get("bootstrap_start", ""), today)
    if forward:
        windows.append(forward)
    if fill_gaps:
        windows += contiguous_runs(excel_store.find_gaps(excel_store.read_frame(path)))
    if not windows:
        log("Already up to date.")
        return _finish(result, path), settings

    total_days = sum((end - start).days for start, end in windows) or 1
    log(f"Need {total_days} day(s) for {ca_no}: " +
        ", ".join(f"{s:%d %b %Y}..{e - timedelta(days=1):%d %b %Y}" for s, e in windows))

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

        fetch = _resilient_fetch(portal, account, settings, password)

        def on_window(start: date, end: date) -> None:
            log(f"Fetching {start:%d %b %Y} .. {end - timedelta(days=1):%d %b %Y}")

        fetched, days_done = FetchResult(), 0
        try:
            for start, end in windows:
                part = fetch_days(fetch, start, end, on_window=on_window)
                fetched.rows += part.rows
                fetched.incomplete_days += part.incomplete_days
                fetched.calls += part.calls
                days_done += (end - start).days
                progress(days_done / total_days)
                if part.error:
                    raise part.error
                if part.stopped:
                    result.stopped = part.stopped
                    log(part.stopped)
                    break
        except Exception:
            # Keep whatever already arrived, then let the caller report the error.
            _save(result, path, fetched, log)
            raise

    _save(result, path, fetched, log)
    return _finish(result, path), settings


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
                    raise SessionExpired("Smart Living keeps logging out -- TNB may be limiting "
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
    result.gaps = [d.isoformat() for d in excel_store.find_gaps(df)]
    return result
