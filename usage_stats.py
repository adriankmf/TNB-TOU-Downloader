"""
usage_stats.py
--------------
Turns an account's Excel rows into the numbers the dashboard draws.

TOU period per half hour:
  * Category from the API when present (e.g. "peak_energy_charge" / "off_peak_energy_charge").
  * Older rows (non-TOU tariff, no Category) use TNB's TOU rule: peak = Mon-Fri 14:00-22:00,
    except Federal/Johor public holidays (whole day off-peak). Those rows are flagged "estimated".
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from tnb_data import COL_CATEGORY, COL_KWH, COL_TIMESTAMP

PEAK_START_HOUR, PEAK_END_HOUR = 14, 22

# Federal + Johor public holidays (all-day off-peak), same list as the old dashboard.
PUBLIC_HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    "2024-01-01 2024-01-23 2024-01-25 2024-02-10 2024-02-11 2024-03-12 2024-03-28 2024-04-10 "
    "2024-04-11 2024-05-01 2024-05-22 2024-06-03 2024-06-17 2024-07-07 2024-08-31 2024-09-15 "
    "2024-09-16 2024-10-31 2024-12-25 2025-01-01 2025-01-23 2025-01-29 2025-01-30 2025-02-11 "
    "2025-03-02 2025-03-31 2025-04-01 2025-04-18 2025-05-01 2025-05-12 2025-06-02 2025-06-07 "
    "2025-06-27 2025-08-31 2025-09-05 2025-09-16 2025-10-20 2025-12-25 2026-01-01 2026-01-17 "
    "2026-01-23 2026-02-17 2026-02-18 2026-02-19 2026-03-20 2026-03-21 2026-04-07 2026-05-01 "
    "2026-05-31 2026-06-01 2026-06-16 2026-07-07 2026-08-31 2026-09-16 2026-11-08 2026-12-25"
).split())

RANGES = {"30": 30, "90": 90, "365": 365, "all": None}


def classify(df: pd.DataFrame) -> pd.DataFrame:
    """Add `peak` (bool) and `estimated` (bool) columns. Returns a new frame."""
    out = df.copy()
    cat = out[COL_CATEGORY].fillna("").astype(str).str.lower()
    ts = out[COL_TIMESTAMP]
    rule_peak = ((ts.dt.weekday < 5) & (ts.dt.hour >= PEAK_START_HOUR) & (ts.dt.hour < PEAK_END_HOUR)
                 & ~ts.dt.date.isin(PUBLIC_HOLIDAYS))
    has_cat = cat.str.contains("peak")
    api_peak = has_cat & ~cat.str.contains("off")
    out["peak"] = api_peak.where(has_cat, rule_peak)
    out["estimated"] = ~has_cat
    return out


def _split(frame: pd.DataFrame, key) -> pd.DataFrame:
    grouped = frame.groupby([key, "peak"])[COL_KWH].sum().unstack(fill_value=0.0)
    for col in (True, False):
        if col not in grouped.columns:
            grouped[col] = 0.0
    return grouped.rename(columns={True: "peak", False: "offpeak"})[["peak", "offpeak"]]


def summarise(df: pd.DataFrame, range_key: str = "30") -> dict:
    """Everything the dashboard needs for one account, JSON-ready."""
    if df.empty:
        return {"empty": True}
    data = classify(df.dropna(subset=[COL_KWH]))
    last_ts = data[COL_TIMESTAMP].max()
    days = RANGES.get(range_key, 30)
    window = data if days is None else data[data[COL_TIMESTAMP] >= last_ts.normalize() - pd.Timedelta(days=days - 1)]

    daily = _split(window, window[COL_TIMESTAMP].dt.date)
    monthly = _split(data, data[COL_TIMESTAMP].dt.to_period("M").astype(str)).tail(24)

    slot = window[COL_TIMESTAMP].dt.hour * 2 + window[COL_TIMESTAMP].dt.minute // 30
    weekend = window[COL_TIMESTAMP].dt.weekday >= 5
    profile = window.groupby([weekend, slot])[COL_KWH].mean().unstack(level=0)

    day_counts = data.groupby(data[COL_TIMESTAMP].dt.date).size()
    complete_days = day_counts[day_counts >= 48].index
    latest_day = max(complete_days) if len(complete_days) else last_ts.date()
    last = data[data[COL_TIMESTAMP].dt.date == latest_day]

    total = float(window[COL_KWH].sum())
    peak_total = float(window.loc[window["peak"], COL_KWH].sum())
    daily_totals = daily.sum(axis=1)
    return {
        "empty": False,
        "range": range_key,
        "kpi": {
            "total_kwh": round(total, 2),
            "avg_daily_kwh": round(float(daily_totals.mean()), 2) if len(daily_totals) else 0,
            "peak_pct": round(100 * peak_total / total, 1) if total else 0,
            "peak_kwh": round(peak_total, 2),
            "offpeak_kwh": round(total - peak_total, 2),
            "max_day": str(daily_totals.idxmax()) if len(daily_totals) else None,
            "max_day_kwh": round(float(daily_totals.max()), 2) if len(daily_totals) else 0,
            "days": int(len(daily_totals)),
            "estimated_pct": round(100 * float(window["estimated"].mean()), 1),
            "first": data[COL_TIMESTAMP].min().strftime("%Y-%m-%d %H:%M"),
            "latest": last_ts.strftime("%Y-%m-%d %H:%M"),
            "rows": int(len(data)),
        },
        "daily": {"labels": [str(d) for d in daily.index],
                  "peak": _r(daily["peak"]), "offpeak": _r(daily["offpeak"])},
        "monthly": {"labels": list(monthly.index),
                    "peak": _r(monthly["peak"]), "offpeak": _r(monthly["offpeak"])},
        "profile": {"labels": [f"{h // 2:02d}:{(h % 2) * 30:02d}" for h in range(48)],
                    "weekday": _r(profile.get(False, pd.Series(dtype=float)).reindex(range(48))),
                    "weekend": _r(profile.get(True, pd.Series(dtype=float)).reindex(range(48)))},
        "latest_day": {"date": str(latest_day),
                       "labels": [t.strftime("%H:%M") for t in last[COL_TIMESTAMP]],
                       "kwh": _r(last[COL_KWH]), "peak": [bool(p) for p in last["peak"]]},
    }


def _r(series: pd.Series) -> list:
    return [None if pd.isna(v) else round(float(v), 3) for v in series]
