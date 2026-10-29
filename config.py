"""
config.py
---------
settings.json next to app.py (or next to TNB_TOU.exe). Holds NO password -- that lives in
Windows Credential Manager (see credentials.py).

Schema:
  email              myTNB login email
  excel_folder       folder holding one tnb_usage_<caNo>.xlsx per account
  selected_account   caNo shown/synced by default
  browser            "chrome" (default) or "edge"
  bootstrap_start    first day to download for an account with no Excel file yet (YYYY-MM-DD)
  accounts           {caNo: {name, guid, smartmeter_url, sdpudcid}} learned from the portal
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

SETTINGS_FILE = APP_DIR / "settings.json"
BROWSER_PROFILE_DIR = APP_DIR / "browser_profile"
LOG_FILE = APP_DIR / "tnb_sync.log"

DEFAULT_BOOTSTRAP_START = "2024-01-01"
BROWSERS = ("chrome", "edge")

# Keys from the cookie/cloudscraper era. Dropped on load so the plaintext password and stale
# cookies never get written back to disk.
LEGACY_KEYS = ("cookie_string", "saved_session_cookie", "password", "smartmeter_url",
               "sdpudcid", "api_base_url", "excel_path", "username", "bootstrap_start_date",
               "bootstrap_days", "request_timeout", "fetch_chunk_days")

DEFAULTS = {
    "email": "",
    "excel_folder": str(APP_DIR),
    "selected_account": "",  # filled with the first account found at sign-in
    "browser": "chrome",
    "bootstrap_start": DEFAULT_BOOTSTRAP_START,
    "accounts": {},
}


def migrate_legacy(raw: dict) -> dict:
    """Map an old settings.json onto the new schema (pure; returns a new dict)."""
    settings = {**DEFAULTS, **{k: v for k, v in raw.items() if k not in LEGACY_KEYS}}
    settings["accounts"] = {k: dict(v) for k, v in (raw.get("accounts") or {}).items()}

    if not settings["email"] and raw.get("username"):
        settings["email"] = raw["username"]
    if raw.get("excel_path") and "excel_folder" not in raw:
        settings["excel_folder"] = str(Path(raw["excel_path"]).parent)
        settings["legacy_excel"] = raw["excel_path"]
    if raw.get("bootstrap_start_date") and "bootstrap_start" not in raw:
        settings["bootstrap_start"] = raw["bootstrap_start_date"]
    if settings["browser"] not in BROWSERS:
        settings["browser"] = "chrome"
    return settings


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("settings.json unreadable (%s) -- using defaults.", exc)
    settings = migrate_legacy(raw)
    if any(key in raw for key in LEGACY_KEYS):
        save_settings(settings, path)
        logger.info("Upgraded settings.json to the new format (old cookie/password keys removed).")
    return settings


def save_settings(settings: dict, path: Path = SETTINGS_FILE) -> None:
    clean = {k: v for k, v in settings.items() if k not in LEGACY_KEYS}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def with_account(settings: dict, ca_no: str, **fields) -> dict:
    """Return a copy of settings with the cached fields of one account updated."""
    accounts = {k: dict(v) for k, v in settings.get("accounts", {}).items()}
    accounts[ca_no] = {**accounts.get(ca_no, {}), **{k: v for k, v in fields.items() if v}}
    return {**settings, "accounts": accounts}
