from datetime import date, datetime

import pandas as pd

import usage_stats
from conftest import make_days


def frame(rows):
    return pd.DataFrame({"Timestamp": [r.timestamp for r in rows], "kWh": [r.kwh for r in rows],
                         "RatePlan": [r.rateplan for r in rows], "Category": [r.category for r in rows]})


def test_classify_uses_category_when_present():
    df = frame(make_days(date(2026, 10, 26), date(2026, 10, 27), category="peak_energy_charge"))
    out = usage_stats.classify(df)
    assert out["peak"].all() and not out["estimated"].any()
    df2 = frame(make_days(date(2026, 10, 26), date(2026, 10, 27), category="off_peak_energy_charge"))
    assert not usage_stats.classify(df2)["peak"].any()


def test_classify_rule_for_old_rows_weekday_weekend_and_holiday():
    # Mon 2026-10-26 (weekday), Sat 2026-10-31 (weekend), Thu 2026-12-25 (holiday)
    rows = []
    for d in (date(2026, 10, 26), date(2026, 10, 31), date(2026, 12, 25)):
        rows += make_days(d, date.fromordinal(d.toordinal() + 1), category="")
    out = usage_stats.classify(frame(rows))
    weekday = out[out["Timestamp"].dt.date == date(2026, 10, 26)]
    assert weekday["peak"].sum() == 16  # 14:00-22:00 = 16 half hours
    assert weekday.loc[weekday["Timestamp"] == datetime(2026, 10, 26, 14, 0), "peak"].item()
    assert not weekday.loc[weekday["Timestamp"] == datetime(2026, 10, 26, 22, 0), "peak"].item()
    assert out[out["Timestamp"].dt.date != date(2026, 10, 26)]["peak"].sum() == 0
    assert out["estimated"].all()


def test_summarise_shapes_and_totals():
    rows = make_days(date(2026, 8, 1), date(2026, 10, 28), kwh=0.5, category="")
    s = usage_stats.summarise(frame(rows), "30")
    assert s["kpi"]["days"] == 30
    assert s["kpi"]["total_kwh"] == 30 * 24.0
    assert s["kpi"]["avg_daily_kwh"] == 24.0
    assert s["kpi"]["latest"] == "2026-10-27 23:30"
    assert len(s["daily"]["labels"]) == 30 and s["daily"]["labels"][-1] == "2026-10-27"
    assert s["monthly"]["labels"] == ["2026-08", "2026-09", "2026-10"]
    assert len(s["profile"]["weekday"]) == 48 and s["profile"]["weekday"][0] == 0.5
    assert s["latest_day"]["date"] == "2026-10-27" and len(s["latest_day"]["kwh"]) == 48
    assert usage_stats.summarise(frame(rows), "all")["kpi"]["days"] == 88


def test_summarise_empty():
    assert usage_stats.summarise(pd.DataFrame(columns=["Timestamp", "kWh", "RatePlan", "Category"]))["empty"]
