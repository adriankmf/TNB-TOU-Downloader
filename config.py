"""
config.py
---------
Loads all runtime configuration from settings.json (written by the GUI).
Falls back to safe defaults for any missing key.

Call config.reload(dict) whenever the user saves new settings in the GUI
— all other modules reference config.* at call-time, so they pick up the
updated values immediately without needing a restart.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# ── App directory (works both as .py and as a PyInstaller .exe) ───────────────
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

SETTINGS_FILE = APP_DIR / "settings.json"


# ── Internal loader ────────────────────────────────────────────────────────────

def _load() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _parse_start_date(raw: str):
    if raw:
        try:
            return datetime.strptime(raw.strip(), "%Y-%m-%d")
        except ValueError:
            pass
    return None


# ── Initial load ───────────────────────────────────────────────────────────────

_s = _load()

TNB_COOKIE_STRING: str  = _s.get("cookie_string", "")
TNB_USERNAME: str       = _s.get("username", "")
TNB_PASSWORD: str       = _s.get("password", "")
TNB_SDPUDCID: str       = _s.get("sdpudcid", "")
TNB_API_BASE_URL: str   = _s.get("api_base_url", "https://smartliving.myaccount.mytnb.com.my")
TNB_LOGIN_PATH: str     = "/User/Login"
TNB_ORG_NAME: str       = "TNB"
TNB_TIMEZONE: str       = "Asia/Kuala_Lumpur"
TNB_DATEFMT_API: str    = "yyyy-MM-dd HH:mm"
TNB_DATEFMT_PY: str     = "%Y-%m-%d %H:%M"
TNB_GRANULARITY: str    = "MIN30"

EXCEL_FILE_PATH: Path   = Path(_s.get("excel_path", str(APP_DIR / "tnb_usage.xlsx")))
BOOTSTRAP_START_DATE    = _parse_start_date(_s.get("bootstrap_start_date", ""))
BOOTSTRAP_DAYS: int     = int(_s.get("bootstrap_days", 30))
REQUEST_TIMEOUT: int    = int(_s.get("request_timeout", 30))


# ── Live reload (called by GUI after user saves settings) ──────────────────────

def reload(new_settings: dict) -> None:
    """
    Update every module-level variable from *new_settings* and persist to disk.
    All other modules read config.* at call-time, so they see the new values
    immediately without restarting.
    """
    global TNB_COOKIE_STRING, TNB_USERNAME, TNB_PASSWORD, TNB_SDPUDCID, TNB_API_BASE_URL
    global EXCEL_FILE_PATH, BOOTSTRAP_START_DATE, BOOTSTRAP_DAYS, REQUEST_TIMEOUT

    # Persist
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(new_settings, f, indent=2, ensure_ascii=False)

    TNB_COOKIE_STRING  = new_settings.get("cookie_string", "")
    TNB_USERNAME       = new_settings.get("username", "")
    TNB_PASSWORD       = new_settings.get("password", "")
    TNB_SDPUDCID       = new_settings.get("sdpudcid", "")
    TNB_API_BASE_URL   = new_settings.get("api_base_url", "https://smartliving.myaccount.mytnb.com.my")
    EXCEL_FILE_PATH    = Path(new_settings.get("excel_path", str(APP_DIR / "tnb_usage.xlsx")))
    BOOTSTRAP_DAYS     = int(new_settings.get("bootstrap_days", 30))
    REQUEST_TIMEOUT    = int(new_settings.get("request_timeout", 30))
    BOOTSTRAP_START_DATE = _parse_start_date(new_settings.get("bootstrap_start_date", ""))
