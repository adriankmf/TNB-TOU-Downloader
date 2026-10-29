from datetime import date, datetime, timedelta

import pytest

from conftest import api_body, make_day, make_days
from tnb_data import (ApiError, ServerError, build_timeseries_path, contiguous_runs, fetch_days, parse_timeseries,
                      plan_chunks, split_unavailable_tail)


def test_path_keeps_literal_plus_between_date_and_time():
    path = build_timeseries_path("40000000000000000001", date(2026, 10, 1), date(2026, 10, 31))
    assert path.startswith("/my_energy_request/timeseries?orgname=TNB&sdpudcid=40000000000000000001")
    assert "dateformat=yyyy-MM-dd+HH:mm" in path
    assert "start=2026-10-01+00:00" in path and "end=2026-10-31+00:00" in path
    assert "granularity=MIN30" in path and "metric=usage" in path
    assert "%2B" not in path


def test_parse_reads_value_rateplan_and_category():
    rows = make_day(date(2026, 10, 27), kwh=0.25, category="peak_energy_charge")
    parsed = parse_timeseries(api_body(rows))
    assert len(parsed) == 48
    assert parsed[0].timestamp == datetime(2026, 10, 27, 0, 0)
    assert parsed[-1].timestamp == datetime(2026, 10, 27, 23, 30)
    assert parsed[0].kwh == 0.25
    assert parsed[0].category == "peak_energy_charge"
    assert parsed[0].rateplan == "TNB NEM Domestic TOU"


def test_parse_skips_bad_points_and_dedupes_and_sorts():
    body = {"data": {"timeseries": [
        {"data": [{"datetime": "2026-10-27 01:00", "value": 1, "metric": "usage"}]},
        {"data": [{"datetime": "2026-10-27 00:30", "value": None}]},
        {"data": [{"datetime": "garbage", "value": 3}]},
        {"data": [{"datetime": "2026-10-27 00:00", "value": "0.4"}]},
        {"data": [{"datetime": "2026-10-27 01:00", "value": 2, "metric": "usage"}]},
        {"data": [{"datetime": "2026-10-27 02:00", "value": 9, "metric": "cost"}]},
        {"data": []},
    ]}}
    parsed = parse_timeseries(body)
    assert [r.timestamp.strftime("%H:%M") for r in parsed] == ["00:00", "01:00"]
    assert parsed[1].kwh == 2.0
    assert parsed[0].category == "" and parsed[0].rateplan == ""


def test_parse_empty_timeseries_is_no_rows():
    assert parse_timeseries({"data": {"timeseries": []}}) == []


@pytest.mark.parametrize("body", [
    {"errorMessage": "Undefined index: view"},
    {"errors": ["bad"], "data": {"timeseries": []}},
    {"data": None},
    {"something": "else"},
    "not a dict",
])
def test_parse_raises_on_error_replies(body):
    with pytest.raises(ApiError):
        parse_timeseries(body)


def test_plan_chunks_caps_windows_at_30_days():
    chunks = plan_chunks(date(2026, 8, 13), date(2026, 10, 29))
    assert chunks[0] == (date(2026, 8, 13), date(2026, 9, 12))
    assert all((e - s).days <= 30 for s, e in chunks)
    assert chunks[-1][1] == date(2026, 10, 29)
    assert all(chunks[i][1] == chunks[i + 1][0] for i in range(len(chunks) - 1))
    assert plan_chunks(date(2026, 1, 1), date(2026, 1, 1)) == []
    with pytest.raises(ValueError):
        plan_chunks(date(2026, 1, 1), date(2026, 1, 2), 0)


def test_contiguous_runs():
    days = [date(2026, 1, 5), date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 2)]
    assert contiguous_runs(days) == [(date(2026, 1, 1), date(2026, 1, 3)), (date(2026, 1, 5), date(2026, 1, 6))]


class FakeApi:
    """Serves rows from a store; optionally misbehaves like the real portal on long windows."""

    def __init__(self, store, truncate_over_days=None):
        self.store, self.truncate_over_days, self.calls = store, truncate_over_days, []

    def __call__(self, start, end):
        self.calls.append((start, end))
        rows = [r for r in self.store if start <= r.timestamp.date() < end]
        if self.truncate_over_days and (end - start).days > self.truncate_over_days:
            rows = [r for r in rows if r.timestamp.date() == start]  # the "77 days -> 1 day" bug
        return rows


def test_fetch_days_complete_range_uses_30_day_calls():
    store = make_days(date(2026, 8, 13), date(2026, 10, 28))
    api = FakeApi(store)
    result = fetch_days(api, date(2026, 8, 13), date(2026, 10, 28))
    assert len(result.rows) == len(store)
    assert result.incomplete_days == []
    assert result.calls == 3


def test_fetch_days_shrinks_window_when_reply_is_silently_truncated():
    store = make_days(date(2026, 8, 1), date(2026, 8, 31))
    api = FakeApi(store, truncate_over_days=10)
    result = fetch_days(api, date(2026, 8, 1), date(2026, 8, 31))
    assert len(result.rows) == 30 * 48
    assert result.incomplete_days == []
    assert (api.calls[1][1] - api.calls[1][0]).days == 15  # retried at half size
    assert len(api.calls) < 15


def test_fetch_days_empty_window_is_not_split():
    api = FakeApi([])
    result = fetch_days(api, date(2023, 1, 1), date(2023, 1, 31))
    assert result.calls == 1
    assert len(result.incomplete_days) == 30


def test_fetch_days_missing_middle_day_is_reported_not_silently_skipped():
    store = [r for r in make_days(date(2026, 8, 1), date(2026, 8, 11)) if r.timestamp.date() != date(2026, 8, 5)]
    api = FakeApi(store)
    result = fetch_days(api, date(2026, 8, 1), date(2026, 8, 11))
    assert result.incomplete_days == [date(2026, 8, 5)]
    assert len(result.rows) == 9 * 48
    assert (date(2026, 8, 5), date(2026, 8, 6)) in api.calls  # retried on its own


def test_fetch_days_keeps_partial_single_day():
    store = make_days(date(2026, 10, 25), date(2026, 10, 27)) + make_day(date(2026, 10, 27), points=20)
    result = fetch_days(FakeApi(store), date(2026, 10, 25), date(2026, 10, 29))
    assert len(result.rows) == 2 * 48 + 20
    assert result.incomplete_days == [date(2026, 10, 27), date(2026, 10, 28)]


def test_fetch_days_ignores_rows_outside_window():
    rows = make_days(date(2026, 1, 1), date(2026, 1, 4))
    result = fetch_days(lambda s, e: rows, date(2026, 1, 2), date(2026, 1, 3))
    assert {r.timestamp.date() for r in result.rows} == {date(2026, 1, 2)}


def test_split_unavailable_tail():
    rows = make_days(date(2026, 10, 1), date(2026, 10, 28))
    incomplete = [date(2026, 10, 5), date(2026, 10, 28), date(2026, 10, 29)]
    gaps, tail = split_unavailable_tail(incomplete, rows)
    assert gaps == [date(2026, 10, 5)]
    assert tail == [date(2026, 10, 28), date(2026, 10, 29)]
    assert split_unavailable_tail([date(2026, 1, 1)], []) == ([], [date(2026, 1, 1)])


def test_fetch_days_progress_callback_sees_every_window():
    seen = []
    fetch_days(FakeApi(make_days(date(2026, 1, 1), date(2026, 3, 1))), date(2026, 1, 1), date(2026, 3, 1),
               on_window=lambda s, e: seen.append((s, e)))
    assert seen[0][0] == date(2026, 1, 1) and seen[-1][1] == date(2026, 3, 1)
    assert len(seen) == 2
    assert timedelta(days=30) >= max(e - s for s, e in seen)


class ErrorApi(FakeApi):
    """FakeApi whose server throws HTTP 500 for any window touching a bad day."""

    def __init__(self, store, bad_days=(), always=False):
        super().__init__(store)
        self.bad_days, self.always = set(bad_days), always

    def __call__(self, start, end):
        self.calls.append((start, end))
        if self.always or any(start <= d < end for d in self.bad_days):
            raise ServerError("HTTP 500")
        return [r for r in self.store if start <= r.timestamp.date() < end]


def test_fetch_days_server_error_on_one_day_keeps_the_rest():
    store = make_days(date(2025, 2, 1), date(2025, 2, 21))
    api = ErrorApi(store, bad_days=[date(2025, 2, 11)])
    result = fetch_days(api, date(2025, 2, 1), date(2025, 2, 21))
    assert len(result.rows) == 19 * 48
    assert result.incomplete_days == [date(2025, 2, 11)]
    assert result.stopped == ""


def test_fetch_days_stops_cleanly_when_server_keeps_failing():
    api = ErrorApi([], always=True)
    result = fetch_days(api, date(2025, 1, 1), date(2025, 6, 1))
    assert result.stopped  # gave up with a message instead of raising
    assert len(api.calls) <= 15


def test_parse_timeseries_accepts_uppercase_metric():
    # Live reply 2026-10-29 had "metric": "USAGE"; lowercase-only matching dropped every point.
    body = {"data": {"timeseries": [{"data": [
        {"datetime": "2026-08-13 00:00", "value": 0.4, "metric": "USAGE", "rateplan": "NEM", "category": "off_peak"},
        {"datetime": "2026-08-13 00:30", "value": 0.3, "metric": "COST"},
    ]}]}}
    rows = parse_timeseries(body)
    assert [r.kwh for r in rows] == [0.4]
