"""
main.py
-------
Entry point for the TNB Smart Sync Data Downloader.

Run with:
    python main.py

The script follows this sequence:
  1. Determine the start datetime (watermark or bootstrap).
  2. Fetch new 30-minute interval data from the myTNB API.
  3. Deduplicate and append only new rows to tnb_usage.xlsx.
  4. Print a human-readable summary.

All heavy logic lives in the domain modules:
  config.py        — configuration / credentials
  tnb_auth.py      — authentication (token management)
  tnb_api.py       — API calls & JSON flattening
  excel_handler.py — Excel read / watermark / append
"""

import logging
import sys
from datetime import datetime

import config          # noqa: F401 — triggers credential validation at import time
import excel_handler
import tnb_api


# ── Logging setup ─────────────────────────────────────────────────────────────
# Use UTF-8 for the stream handler so Unicode chars (arrows, tick marks) don't
# crash on Windows terminals that default to CP1252.
_handler = logging.StreamHandler(stream=sys.stdout)
_handler.setFormatter(logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
try:
    # Python 3.9+ supports encoding= on StreamHandler
    import io
    _handler.stream = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
except AttributeError:
    pass  # Python <3.9 or no buffer — use as-is, unicode chars will be replaced

logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger(__name__)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 60)
    logger.info("TNB Smart Sync — starting run at %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    # ── Step 1: Determine fetch window ────────────────────────────────────
    start_dt = excel_handler.get_start_datetime()
    end_dt   = datetime.now()

    # Guard: if start is already in the future (shouldn't happen normally)
    # or within the last interval, there is nothing to fetch yet.
    from datetime import timedelta
    if start_dt >= end_dt - timedelta(minutes=excel_handler.INTERVAL_MINUTES):
        logger.info(
            "Start datetime (%s) is within one interval of now (%s). "
            "No new data available yet — try again after the next 30-minute window.",
            start_dt.strftime("%Y-%m-%d %H:%M"),
            end_dt.strftime("%Y-%m-%d %H:%M"),
        )
        _print_summary(0)
        return

    # ── Step 2: Fetch from API ────────────────────────────────────────────
    try:
        new_data_df = tnb_api.get_usage_dataframe(start_dt, end_dt)
    except Exception as exc:
        logger.error("Failed to fetch data from myTNB API: %s", exc)
        logger.error("Aborting run — no changes made to the Excel file.")
        sys.exit(1)

    # ── Step 3: Handle "no new data" gracefully ───────────────────────────
    if new_data_df.empty:
        logger.info(
            "The API returned no records for the window %s → %s. "
            "This is expected if you have already synced recently.",
            start_dt.strftime("%Y-%m-%d %H:%M"),
            end_dt.strftime("%Y-%m-%d %H:%M"),
        )
        _print_summary(0)
        return

    # ── Step 4: Append to Excel ───────────────────────────────────────────
    try:
        rows_added = excel_handler.append_new_rows(new_data_df)
    except Exception as exc:
        logger.error("Failed to write data to Excel: %s", exc)
        logger.error("Aborting run — the Excel file may be in an inconsistent state.")
        sys.exit(1)

    _print_summary(rows_added)


def _print_summary(rows_added: int) -> None:
    """Print the end-of-run summary that satisfies the acceptance criteria."""
    logger.info("-" * 60)
    if rows_added == 0:
        logger.info("SUMMARY: No new records added (data is already up to date).")
    else:
        logger.info("SUMMARY: Added %d new record(s) to '%s'.", rows_added, config.EXCEL_FILE_PATH)
    logger.info("-" * 60)


if __name__ == "__main__":
    main()
