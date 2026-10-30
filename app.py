"""
app.py
------
TNB Smart Sync desktop app: a pywebview window (ui/index.html) backed by the Api class below.
Every public Api method is callable from JavaScript as `pywebview.api.<name>(...)` and returns
a JSON-friendly dict: {"ok": True, ...} or {"ok": False, "error": "..."}.

Run:  python app.py   (or "Start TNB Sync.bat")
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import asdict
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import webview

import config
import credentials
import excel_store
import sync
import usage_stats
from excel_store import ExcelLockedError
from tnb_data import ApiError
from tnb_portal import PortalError

logger = logging.getLogger("tnb")

UI_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "ui"


def setup_logging() -> None:
    handler = RotatingFileHandler(config.LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


class Api:
    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._busy = threading.Lock()
        self._settings = config.load_settings()
        self._frame_cache: tuple = (None, None)
        self._stop = threading.Event()  # set by the Stop button; checked between requests

    def attach(self, window: webview.Window) -> None:
        self._window = window

    # -- plumbing --

    def _emit(self, kind: str, payload) -> None:
        if self._window is not None:
            self._window.evaluate_js(f"window.tnb && window.tnb.on({json.dumps(kind)}, {json.dumps(payload)})")

    def _log(self, message: str) -> None:
        logger.info(message)
        self._emit("log", {"time": datetime.now().strftime("%H:%M:%S"), "text": message})

    def _save(self, settings: dict) -> None:
        self._settings = settings
        config.save_settings(settings)

    def _run(self, label: str, work):
        """Run one browser/file task at a time and turn errors into friendly messages."""
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "Another task is still running -- please wait."}
        try:
            return {"ok": True, **work()}
        except (PortalError, ApiError, ExcelLockedError, ValueError) as exc:
            logger.warning("%s failed: %s", label, exc)
            self._log(f"{label} failed: {exc}")
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 -- last line of defence for the UI
            logger.exception("%s crashed", label)
            self._log(f"{label} failed unexpectedly: {exc}")
            return {"ok": False, "error": f"Unexpected error: {exc} (details in tnb_sync.log)"}
        finally:
            self._busy.release()

    def _accounts_view(self) -> list[dict]:
        folder = self._settings["excel_folder"]
        view = []
        for ca_no, acc in self._settings.get("accounts", {}).items():
            path = excel_store.account_file(folder, ca_no)
            view.append({"ca_no": ca_no, "name": acc.get("name") or ca_no,
                         "file": str(path), "has_file": path.exists()})
        selected = self._settings.get("selected_account")
        return sorted(view, key=lambda a: (a["ca_no"] != selected, a["name"].lower()))

    # -- state --

    def get_state(self) -> dict:
        s = self._settings
        return {
            "ok": True,
            "email": s.get("email", ""),
            "signed_in": bool(s.get("email") and credentials.get_password(s["email"])),
            "accounts": self._accounts_view(),
            "selected_account": s.get("selected_account", ""),
            "excel_folder": s["excel_folder"],
            "browser": s.get("browser", "chrome"),
            "bootstrap_start": s.get("bootstrap_start") or config.default_bootstrap_start(date.today()),
            "setup_done": bool(s.get("setup_done")),
            "busy": self._busy.locked(),
        }

    def startup(self) -> dict:
        """Called once when the page loads: one-off migration of the old Excel file."""
        def work():
            self._save(sync.migrate_if_needed(self._settings, self._log))
            return self.get_state()
        return self._run("Startup", work)

    # -- login / accounts --

    def login(self, email: str, password: str) -> dict:
        email, password = (email or "").strip(), password or ""
        if "@" not in email or not password:
            return {"ok": False, "error": "Enter your myTNB email and password."}

        def work():
            self._log("Signing in to myTNB (no browser window needed)...")
            accounts, settings, mode = sync.refresh_accounts({**self._settings, "email": email}, password, log=self._log)
            credentials.set_password(email, password)  # saved only after myTNB accepted it
            self._save(settings)
            self._log(f"Signed in. Found {len(accounts)} account(s).")
            return self.get_state()

        return self._run("Sign-in", work)

    def refresh_accounts(self) -> dict:
        def work():
            accounts, settings, _ = sync.refresh_accounts(self._settings, self._password(), log=self._log)
            self._save(settings)
            self._log(f"Account list updated ({len(accounts)} account(s)).")
            return self.get_state()
        return self._run("Refresh accounts", work)

    def sign_out(self) -> dict:
        credentials.delete_password(self._settings.get("email", ""))
        self._log("Signed out -- saved password removed from Windows Credential Manager.")
        return self.get_state()

    def _password(self) -> str:
        password = credentials.get_password(self._settings.get("email", ""))
        if not password:
            raise ValueError("No saved password -- please sign in again.")
        return password

    # -- settings --

    def select_account(self, ca_no: str) -> dict:
        if ca_no not in self._settings.get("accounts", {}):
            return {"ok": False, "error": "Unknown account."}
        self._save({**self._settings, "selected_account": ca_no})
        return self.get_state()

    def save_prefs(self, prefs: dict) -> dict:
        prefs = prefs or {}
        browser = prefs.get("browser", self._settings.get("browser"))
        start = prefs.get("bootstrap_start", self._settings.get("bootstrap_start"))
        if browser not in config.BROWSERS:
            return {"ok": False, "error": "Browser must be Chrome or Edge."}
        try:
            start = config.parse_bootstrap_start(start, date.today())
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        self._save({**self._settings, "browser": browser, "bootstrap_start": start})
        return self.get_state()

    def finish_setup(self, prefs: dict) -> dict:
        """First-run screen: the Excel folder is already set by choose_folder; save the date."""
        try:
            start = config.parse_bootstrap_start((prefs or {}).get("bootstrap_start"), date.today())
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        self._save({**self._settings, "bootstrap_start": start, "setup_done": True})
        self._log(f"Setup saved. Excel folder: {self._settings['excel_folder']}. "
                  f"First download from {start or config.default_bootstrap_start(date.today())}.")
        return self.get_state()

    def choose_folder(self) -> dict:
        picked = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=self._settings["excel_folder"])
        if not picked:
            return self.get_state()
        self._save({**self._settings, "excel_folder": str(picked[0])})
        self._log(f"Excel folder set to {picked[0]}")
        return self.get_state()

    def open_path(self, which: str, ca_no: str = "") -> dict:
        target = (excel_store.account_file(self._settings["excel_folder"], ca_no) if which == "file"
                  else Path(self._settings["excel_folder"]) if which == "folder" else config.LOG_FILE)
        if not Path(target).exists():
            return {"ok": False, "error": f"{target} does not exist yet."}
        os.startfile(target)  # noqa: S606 -- local file chosen by the app, not by input text
        return {"ok": True}

    # -- sync + dashboard --

    def sync(self, ca_no: str, fill_gaps: bool = False) -> dict:
        """Download everything missing, batch by batch with pauses (sync.download_all).
        Can run for a long time on a first download of several years; stop_sync() ends it."""
        def work():
            name = self._settings.get("accounts", {}).get(ca_no, {}).get("name", ca_no)
            self._log(f"Sync started for {name} ({ca_no}){' with gap fill' if fill_gaps else ''}.")
            self._stop.clear()
            result, settings = sync.download_all(
                self._settings, ca_no, self._password(), fill_gaps=fill_gaps,
                save=self._batch_saved, log=self._log, should_stop=self._stop.is_set,
                sleep=self._stop.wait,  # wakes at once when Stop is pressed
                status=lambda text: self._emit("status", text),
                progress=lambda p: self._emit("progress", round(p, 3)))
            self._merge_sync_settings(settings)
            summary = f"Done: {result.rows_added} new reading(s), data from {result.first or '-'} to {result.latest or '-'}."
            if result.gaps:
                summary += f" {len(result.gaps)} incomplete day(s) in history."
            if result.stopped:
                summary += f" {result.stopped}"
            self._log(summary)
            return {"result": asdict(result), "state": self.get_state()}
        return self._run("Sync", work)

    def stop_sync(self) -> dict:
        if self._busy.locked():
            self._stop.set()
            self._log("Stopping after the current request...")
        return {"ok": True}

    def _batch_saved(self, settings: dict) -> None:
        self._merge_sync_settings(settings)
        self._emit("batch", None)  # the dashboard reloads to show the new days

    def _merge_sync_settings(self, settings: dict) -> None:
        """Take only the account cache from a sync: the user may change other settings
        (selected account, browser...) while a long download is running."""
        accounts = {**self._settings.get("accounts", {}), **settings.get("accounts", {})}
        self._save({**self._settings, "accounts": accounts})

    def _frame(self, path: Path):
        """read_frame with a one-file cache, so switching the date range doesn't re-read Drive."""
        stamp = (str(path), path.stat().st_mtime if path.exists() else None)
        if self._frame_cache[0] != stamp:
            self._frame_cache = (stamp, excel_store.read_frame(path))
        return self._frame_cache[1]

    def usage(self, ca_no: str, range_key: str = "30") -> dict:
        try:
            path = excel_store.account_file(self._settings["excel_folder"], ca_no)
            df = self._frame(path)
            stats = usage_stats.summarise(df, range_key)
            stats["gaps"] = [d.isoformat() for d in excel_store.find_gaps(df)]
            stats["file"] = str(path)
            return {"ok": True, **stats}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Reading usage failed")
            return {"ok": False, "error": f"Could not read the Excel file: {exc}"}


def main() -> None:
    setup_logging()
    api = Api()
    window = webview.create_window(
        "TNB Smart Sync", url=str(UI_DIR / "index.html"), js_api=api,
        width=1320, height=900, min_size=(980, 680), background_color="#0f1419")
    api.attach(window)
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
