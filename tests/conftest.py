import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tnb_data import UsageRow  # noqa: E402


def make_day(day: date, kwh: float = 0.5, category: str = "off_peak_energy_charge",
             rateplan: str = "TNB NEM Domestic TOU", points: int = 48) -> list[UsageRow]:
    start = datetime.combine(day, datetime.min.time())
    return [UsageRow(start + timedelta(minutes=30 * i), kwh, rateplan, category) for i in range(points)]


def make_days(start: date, end: date, **kw) -> list[UsageRow]:
    rows = []
    d = start
    while d < end:
        rows += make_day(d, **kw)
        d += timedelta(days=1)
    return rows


def api_body(rows: list[UsageRow]) -> dict:
    """Shape of the real /timeseries reply (one slot per half hour)."""
    return {"data": {"timeseries": [
        {"start": f"{r.timestamp:%Y-%m-%d %H:%M}", "data": [{
            "category": r.category, "datetime": f"{r.timestamp:%Y-%m-%d %H:%M}", "flag": "A",
            "granularity": "MIN30", "metric": "usage", "rateplan": r.rateplan, "value": r.kwh}]}
        for r in rows]}}


@pytest.fixture
def day_rows():
    return make_day
