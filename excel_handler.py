"""
excel_handler.py
----------------
All Excel / file-system concerns live here.

Responsibilities:
  • Determine whether the Excel database already exists.
  • Read the watermark (last recorded Timestamp) so the caller knows what to
    fetch next.
  • Deduplicate incoming data against what is already stored.
  • Append new rows and save — without touching or re-writing existing rows.

This module has NO knowledge of HTTP, credentials, or the myTNB API; it only
reads and writes DataFrames to/from disk.

Excel file schema (updated — kW_peak removed, RatePlan added)
-----------------
Sheet name : "Usage"
Columns    : Timestamp | kWh | RatePlan
             (datetime) (float) (str)
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from openpyxl import load_workbook

import config
from tnb_api import COL_TIMESTAMP, COL_KWH, COL_RATEPLAN

logger = logging.getLogger(__name__)

SHEET_NAME       = "Usage"
INTERVAL_MINUTES = 30


# ── Public interface ──────────────────────────────────────────────────────────

def get_start_datetime() -> datetime:
    """
    Return the datetime from which new data should be fetched.

    Logic
    -----
    • If no Excel file exists   → bootstrap: return (now − BOOTSTRAP_DAYS days).
    • If the file exists        → watermark: return (last Timestamp + 30 min).

    The "+30 min" shift ensures we never re-request the last saved interval.
    The deduplication in append_new_rows() is a secondary safety net.
    """
    path = config.EXCEL_FILE_PATH

    if not path.exists():
        start = _bootstrap_start()
        logger.info(
            "No Excel file found at '%s'. Bootstrapping from %s.",
            path, start.strftime("%Y-%m-%d %H:%M"),
        )
        return start

    last_ts = _read_watermark(path)
    if last_ts is None:
        start = _bootstrap_start()
        logger.info(
            "Excel file exists but contains no data. Bootstrapping from %s.",
            start.strftime("%Y-%m-%d %H:%M"),
        )
        return start

    start = last_ts + timedelta(minutes=INTERVAL_MINUTES)
    logger.info(
        "Watermark found: last entry at %s. "
        "Will fetch data from %s onwards.",
        last_ts.strftime("%Y-%m-%d %H:%M"),
        start.strftime("%Y-%m-%d %H:%M"),
    )
    return start


def append_new_rows(new_df: pd.DataFrame) -> int:
    """
    Deduplicate *new_df* against the existing Excel file and append only truly
    new rows.  Creates the file from scratch if it does not yet exist.

    Parameters
    ----------
    new_df : pd.DataFrame
        Must contain columns: Timestamp, kWh, RatePlan.

    Returns
    -------
    int
        Number of rows actually written (0 if nothing was new).
    """
    path = config.EXCEL_FILE_PATH

    if new_df.empty:
        logger.info("append_new_rows called with an empty DataFrame — nothing to write.")
        return 0

    new_df = _normalise_df(new_df)

    existing_timestamps: set = _load_existing_timestamps(path)
    mask_new   = ~new_df[COL_TIMESTAMP].isin(existing_timestamps)
    rows_to_add = new_df[mask_new].copy()

    if rows_to_add.empty:
        logger.info(
            "All %d record(s) from the API are already present in the Excel file. "
            "Nothing to append.",
            len(new_df),
        )
        return 0

    dupes = len(new_df) - len(rows_to_add)
    if dupes:
        logger.info("Dropped %d duplicate record(s) already in the file.", dupes)

    if not path.exists():
        _create_new_excel(rows_to_add, path)
    else:
        _append_to_existing_excel(rows_to_add, path)

    logger.info("Successfully wrote %d new row(s) to '%s'.", len(rows_to_add), path)
    return len(rows_to_add)


# ── Private helpers ───────────────────────────────────────────────────────────

def _bootstrap_start() -> datetime:
    """Return the configured bootstrap start datetime."""
    if config.BOOTSTRAP_START_DATE:
        return config.BOOTSTRAP_START_DATE
    return datetime.now() - timedelta(days=config.BOOTSTRAP_DAYS)


def _read_watermark(path: Path) -> Optional[datetime]:
    """Return the maximum Timestamp in the Excel file, or None if empty."""
    try:
        df = pd.read_excel(
            path,
            sheet_name=SHEET_NAME,
            usecols=[COL_TIMESTAMP],
            parse_dates=[COL_TIMESTAMP],
        )
    except Exception as exc:
        logger.warning("Could not read watermark from '%s': %s. Bootstrapping instead.", path, exc)
        return None

    if df.empty or df[COL_TIMESTAMP].dropna().empty:
        return None

    max_ts = df[COL_TIMESTAMP].dropna().max()
    return max_ts.to_pydatetime() if hasattr(max_ts, "to_pydatetime") else max_ts


def _load_existing_timestamps(path: Path) -> set:
    """Return the set of all Timestamps already stored in the Excel file."""
    if not path.exists():
        return set()
    try:
        df = pd.read_excel(
            path,
            sheet_name=SHEET_NAME,
            usecols=[COL_TIMESTAMP],
            parse_dates=[COL_TIMESTAMP],
        )
        return set(df[COL_TIMESTAMP].dropna())
    except Exception as exc:
        logger.warning("Could not load existing timestamps: %s. Deduplication skipped.", exc)
        return set()


def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce column types and sort chronologically."""
    df = df.copy()
    df[COL_TIMESTAMP] = pd.to_datetime(df[COL_TIMESTAMP])
    df[COL_KWH]       = pd.to_numeric(df[COL_KWH], errors="coerce")
    # RatePlan is a string — fill blanks with empty string.
    if COL_RATEPLAN in df.columns:
        df[COL_RATEPLAN] = df[COL_RATEPLAN].fillna("").astype(str)
    else:
        df[COL_RATEPLAN] = ""
    return df.sort_values(COL_TIMESTAMP).reset_index(drop=True)


def _create_new_excel(df: pd.DataFrame, path: Path) -> None:
    """Write df as the first data in a brand-new Excel file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Creating new Excel file at '%s'…", path)
    with pd.ExcelWriter(path, engine="openpyxl", datetime_format="YYYY-MM-DD HH:MM:SS") as writer:
        df.to_excel(writer, sheet_name=SHEET_NAME, index=False)
    logger.debug("New file written with %d row(s).", len(df))


def _append_to_existing_excel(df: pd.DataFrame, path: Path) -> None:
    """
    Append rows to the existing sheet WITHOUT re-writing the whole file.

    Uses openpyxl directly so that any charts, formatting, or extra sheets
    the user has added are preserved.
    """
    workbook = load_workbook(path)

    if SHEET_NAME not in workbook.sheetnames:
        logger.warning("Sheet '%s' missing — recreating it.", SHEET_NAME)
        workbook.create_sheet(SHEET_NAME)
        sheet = workbook[SHEET_NAME]
        sheet.append([COL_TIMESTAMP, COL_KWH, COL_RATEPLAN])
    else:
        sheet = workbook[SHEET_NAME]

    for _, row in df.iterrows():
        ts = row[COL_TIMESTAMP]
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()

        sheet.append([
            ts,
            float(row[COL_KWH]) if pd.notna(row[COL_KWH]) else None,
            str(row[COL_RATEPLAN]) if row[COL_RATEPLAN] else "",
        ])

    workbook.save(path)
    logger.debug("Appended %d row(s) to existing file.", len(df))
