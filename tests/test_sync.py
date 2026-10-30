from datetime import date, datetime

import pytest

import config
import excel_store
import sync
from conftest import make_day, make_days
from tnb_data import ServerError
from tnb_portal import SessionExpired, TnbThrottled

CA = "220000000001"


class FakePortal:
    """Stands in for TnbPortal: serves rows from memory, records what was asked."""

    def __init__(self, store, expire_once=False, bad_days=(), expire_times=0):
        self.store, self.bad_days = store, set(bad_days)
        self.expire_left = expire_times or (1 if expire_once else 0)
        self.windows, self.logins, self.opened, self.recovered = [], 0, 0, 0

    def __call__(self, settings, log):  # acts as portal_factory
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def ensure_login(self, email, password):
        self.logins += 1
        return "session"

    def list_accounts(self):
        return [{"ca_no": "210000000002", "name": "Farm", "guid": "g1"},
                {"ca_no": CA, "name": "Home", "guid": "g2"}]

    def open_smart_living(self, account):
        self.opened += 1
        return {"smartmeter_url": "https://link/" + account["ca_no"], "sdpudcid": "4000"}

    def recover_data_session(self, sdpudcid):
        self.recovered += 1

    def fetch_rows(self, sdpudcid, start, end):
        if self.expire_left:
            self.expire_left -= 1
            raise SessionExpired("gone")
        self.windows.append((start, end))
        if any(start <= d < end for d in self.bad_days):
            raise ServerError("HTTP 500")
        return [r for r in self.store if start <= r.timestamp.date() < end]


@pytest.fixture
def settings(tmp_path):
    return {**config.DEFAULTS, "excel_folder": str(tmp_path), "bootstrap_start": "2026-09-01", "accounts": {},
            "selected_account": CA}


def run(settings, portal, **kw):
    return sync.sync_account(settings, CA, "pw", portal_factory=portal, today=date(2026, 10, 28), **kw)


def test_first_sync_bootstraps_and_caches_account(settings):
    store = make_days(date(2026, 9, 1), date(2026, 10, 28))
    portal = FakePortal(store)
    result, new_settings = run(settings, portal)
    assert result.rows_added == len(store)
    assert result.latest == "2026-10-27 23:30"
    assert result.pending == ["2026-10-28"] and result.gaps == [] and result.unavailable == []
    assert new_settings["accounts"][CA]["sdpudcid"] == "4000"
    assert new_settings["accounts"][CA]["guid"] == "g2"
    assert settings["accounts"] == {}  # input not mutated
    assert all((e - s).days <= 30 for s, e in portal.windows)


def test_incremental_sync_starts_after_watermark_without_duplicates(settings, tmp_path):
    path = excel_store.account_file(tmp_path, CA)
    excel_store.append_rows(path, make_days(date(2026, 8, 1), date(2026, 8, 13)))
    store = make_days(date(2026, 8, 1), date(2026, 10, 28))
    portal = FakePortal(store)
    result, _ = run(settings, portal)
    assert portal.windows[0][0] == date(2026, 8, 13)
    assert result.rows_added == (date(2026, 10, 28) - date(2026, 8, 13)).days * 48
    df = excel_store.read_frame(path)
    assert df["Timestamp"].is_unique and len(df) == (date(2026, 10, 28) - date(2026, 8, 1)).days * 48

    again, _ = run(settings, FakePortal(store))
    assert again.rows_added == 0


def test_partial_last_day_is_completed_next_time(settings, tmp_path):
    path = excel_store.account_file(tmp_path, CA)
    excel_store.append_rows(path, make_days(date(2026, 10, 1), date(2026, 10, 27))
                            + make_day(date(2026, 10, 27), points=20))
    portal = FakePortal(make_days(date(2026, 10, 1), date(2026, 10, 28)))
    result, _ = run(settings, portal)
    assert portal.windows[0][0] == date(2026, 10, 27)
    assert result.rows_added == 28
    assert excel_store.watermark(path) == datetime(2026, 10, 27, 23, 30)


def test_up_to_date_does_not_open_browser(settings, tmp_path):
    path = excel_store.account_file(tmp_path, CA)
    excel_store.append_rows(path, make_day(date(2026, 10, 28)))
    portal = FakePortal([])
    result, _ = run({**settings, "bootstrap_start": "2026-10-28"}, portal)
    assert portal.logins == 0 and result.rows_added == 0


def test_missing_day_is_reported_and_fill_gaps_retries_it(settings):
    full = make_days(date(2026, 10, 1), date(2026, 10, 28))
    holey = [r for r in full if r.timestamp.date() != date(2026, 10, 10)]
    oct_settings = {**settings, "bootstrap_start": "2026-10-01"}
    result, _ = run(oct_settings, FakePortal(holey))
    assert result.unavailable == ["2026-10-10"] and result.gaps == ["2026-10-10"]

    portal = FakePortal(full)
    filled, _ = run(oct_settings, portal, fill_gaps=True)
    assert (date(2026, 10, 10), date(2026, 10, 11)) in portal.windows
    assert filled.rows_added == 48 and filled.gaps == []


def test_session_drop_mid_sync_waits_in_place_before_logging_in_again(settings):
    portal = FakePortal(make_days(date(2026, 10, 20), date(2026, 10, 28)), expire_once=True)
    result, _ = run({**settings, "bootstrap_start": "2026-10-20"}, portal)
    assert portal.recovered == 1 and portal.opened == 1 and result.rows_added == 8 * 48


def test_session_still_dropped_after_waiting_reopens_smart_living(settings):
    portal = FakePortal(make_days(date(2026, 10, 20), date(2026, 10, 28)), expire_times=2)
    result, _ = run({**settings, "bootstrap_start": "2026-10-20"}, portal)
    assert portal.recovered == 1 and portal.opened == 2 and result.rows_added == 8 * 48


class FlakyPortal(FakePortal):
    """Session drops on every call matching `fails(call_no)` (1-based)."""

    def __init__(self, store, fails):
        super().__init__(store)
        self.fails, self.calls = fails, 0

    def fetch_rows(self, sdpudcid, start, end):
        self.calls += 1
        if self.fails(self.calls):
            raise SessionExpired("gone")
        return super().fetch_rows(sdpudcid, start, end)


def test_dead_session_is_reported_as_throttled_and_keeps_fetched_rows(settings, tmp_path):
    # Recent 30 days arrive, then the session never comes back for the history window.
    portal = FlakyPortal(make_days(date(2026, 9, 1), date(2026, 10, 29)), fails=lambda n: n > 1)
    result, _ = run(settings, portal)
    assert result.throttled and result.more and "keeps logging out" in result.stopped
    saved = excel_store.read_frame(excel_store.account_file(tmp_path, CA))
    assert len(saved) == 30 * 48
    assert portal.recovered == 1 and portal.opened == 2


def test_sync_never_logs_in_again_more_than_once(settings, tmp_path):
    # Every first and second try fails, so each window would need a full re-login.
    portal = FlakyPortal(make_days(date(2026, 5, 1), date(2026, 10, 29)), fails=lambda n: n % 3 != 0)
    result, _ = run({**settings, "bootstrap_start": "2026-05-01"}, portal)
    assert result.throttled
    assert portal.opened == 1 + sync.MAX_RELOGINS
    assert portal.logins == 1 + sync.MAX_RELOGINS


def test_flapping_session_stops_after_recovery_limit(settings, tmp_path):
    # Every window's first try fails: each costs one recovery until the limit is used up.
    portal = FlakyPortal(make_days(date(2026, 5, 1), date(2026, 10, 29)), fails=lambda n: n % 2 == 1)
    result, _ = run({**settings, "bootstrap_start": "2026-05-01"}, portal)
    assert result.throttled
    saved = excel_store.read_frame(excel_store.account_file(tmp_path, CA))
    assert portal.recovered == sync.MAX_SESSION_RECOVERIES
    assert len(saved) == sync.MAX_SESSION_RECOVERIES * 30 * 48


def test_custom_folder_overrides_settings(settings, tmp_path):
    other = tmp_path / "copy"
    result, _ = run(settings, FakePortal(make_day(date(2026, 10, 27))), folder=str(other))
    assert result.file == str(excel_store.account_file(other, CA))


def test_refresh_accounts_caches_and_keeps_selection(settings):
    accounts, new_settings, mode = sync.refresh_accounts(settings, "pw", portal_factory=FakePortal([]))
    assert [a["ca_no"] for a in accounts] == ["210000000002", CA]
    assert new_settings["selected_account"] == CA and mode == "session"
    assert new_settings["accounts"]["210000000002"]["name"] == "Farm"


def test_migrate_if_needed_moves_legacy_file(settings, tmp_path):
    legacy = tmp_path / "tnb_usage.xlsx"
    excel_store.append_rows(legacy, make_day(date(2026, 8, 12)))
    out = sync.migrate_if_needed({**settings, "legacy_excel": str(legacy)})
    assert "legacy_excel" not in out
    assert excel_store.account_file(tmp_path, CA).exists() and not legacy.exists()


def test_fill_gaps_fetches_new_data_first_and_survives_server_error(settings, tmp_path):
    path = excel_store.account_file(tmp_path, CA)
    old = [r for r in make_days(date(2026, 9, 1), date(2026, 10, 20)) if r.timestamp.date() != date(2026, 9, 10)]
    excel_store.append_rows(path, old)
    portal = FakePortal(make_days(date(2026, 9, 1), date(2026, 10, 28)), bad_days=[date(2026, 9, 10)])
    result, _ = run(settings, portal, fill_gaps=True)
    assert portal.windows[0][0] == date(2026, 10, 20)       # newest data asked for first
    assert result.latest == "2026-10-27 23:30"               # new data saved despite the 500
    assert result.unavailable == ["2026-09-10"]


def test_migrate_if_needed_waits_until_an_account_is_known(settings, tmp_path):
    legacy = tmp_path / "tnb_usage.xlsx"
    excel_store.append_rows(legacy, make_day(date(2026, 8, 12)))
    out = sync.migrate_if_needed({**settings, "selected_account": "", "legacy_excel": str(legacy)})
    assert out["legacy_excel"] == str(legacy) and legacy.exists()


def test_refresh_accounts_selects_first_account_when_none_selected(settings):
    _, new_settings, _ = sync.refresh_accounts({**settings, "selected_account": ""}, "pw",
                                               portal_factory=FakePortal([]))
    assert new_settings["selected_account"] == "210000000002"


def test_first_sync_without_chosen_date_goes_two_months_back(settings, tmp_path):
    forward, backward = sync.plan_windows(tmp_path / "none.xlsx", {**settings, "bootstrap_start": ""},
                                          CA, date(2026, 10, 30))
    assert forward == [(date(2026, 10, 1), date(2026, 10, 31))]      # last 30 days first
    assert backward == [(date(2026, 9, 1), date(2026, 10, 1)),       # then back to 2 months,
                        (date(2026, 8, 31), date(2026, 9, 1))]        # newest first


# -- Batched history download -------------------------------------------------------------------

def test_years_of_history_are_split_into_batches_newest_first(settings, tmp_path):
    portal = FakePortal(make_days(date(2024, 1, 1), date(2026, 10, 29)))
    result, _ = run({**settings, "bootstrap_start": "2024-01-01"}, portal)
    assert result.more                                                   # more batches needed
    asked = sum((e - s).days for s, e in portal.windows)
    assert asked <= sync.BATCH_DAYS and len(portal.windows) <= sync.BATCH_DAYS // 30
    assert portal.windows[0] == (date(2026, 9, 29), date(2026, 10, 29))  # newest first
    starts = [s for s, _ in portal.windows]
    assert starts == sorted(starts, reverse=True)                        # then walking back
    assert excel_store.find_gaps(excel_store.read_frame(excel_store.account_file(tmp_path, CA))) == []


class StopAfter:
    def __init__(self):
        self.stop, self.slept = False, 0

    def sleep(self, seconds):
        self.slept += seconds


@pytest.fixture(autouse=False)
def small_batches(monkeypatch):
    """60-day batches keep the multi-batch tests fast (each batch rewrites the Excel file)."""
    monkeypatch.setattr(sync, "BATCH_DAYS", 60)


def run_all(settings, portal, **kw):
    saved = []
    result, new = sync.download_all(settings, CA, "pw", portal_factory=portal, today=date(2026, 10, 28),
                                    save=saved.append, **kw)
    return result, new, saved


def test_download_all_fetches_years_in_batches_with_pauses_and_one_browser(settings, tmp_path, small_batches):
    store = make_days(date(2026, 5, 1), date(2026, 10, 29))
    portal = CountingPortal(store)
    clock = StopAfter()
    result, new, saved = run_all({**settings, "bootstrap_start": "2026-05-01"}, portal, sleep=clock.sleep)
    days = (date(2026, 10, 29) - date(2026, 5, 1)).days
    batches = -(-days // sync.BATCH_DAYS)
    assert not result.more and not result.stopped
    assert result.first == "2026-05-01 00:00" and result.rows_added == len(store)
    assert len(saved) == batches                                         # settings saved per batch
    assert clock.slept == (batches - 1) * sync.BATCH_PAUSE_S
    assert portal.entered == 1 and portal.exited == 1                    # one browser for the run


def test_download_all_waits_and_continues_when_tnb_limits(settings, tmp_path, small_batches):
    store = make_days(date(2026, 7, 1), date(2026, 10, 29))
    portal = CountingPortal(store, throttle_on_call=3)          # 3rd request: TNB says "slow down"
    clock = StopAfter()
    result, _, _ = run_all({**settings, "bootstrap_start": "2026-07-01"}, portal, sleep=clock.sleep)
    assert result.first == "2026-07-01 00:00" and not result.more
    batches = -(-(date(2026, 10, 29) - date(2026, 7, 1)).days // sync.BATCH_DAYS)
    assert clock.slept == sync.THROTTLE_PAUSE_S + (batches - 1) * sync.BATCH_PAUSE_S


def test_download_all_gives_up_after_repeated_limits(settings, tmp_path, small_batches):
    portal = CountingPortal(make_days(date(2026, 6, 1), date(2026, 10, 29)), throttle_on_call=lambda n: n > 1)
    clock = StopAfter()
    result, _, _ = run_all({**settings, "bootstrap_start": "2026-06-01"}, portal, sleep=clock.sleep)
    assert "keeps limiting" in result.stopped
    assert clock.slept == sync.MAX_THROTTLE_PAUSES * sync.THROTTLE_PAUSE_S


def test_stop_during_pause_ends_run_and_keeps_data(settings, tmp_path, small_batches):
    portal = CountingPortal(make_days(date(2026, 5, 1), date(2026, 10, 29)))
    clock = StopAfter()

    def sleep(seconds):
        clock.sleep(seconds)
        clock.stop = clock.slept >= 60

    result, _, saved = run_all({**settings, "bootstrap_start": "2026-05-01"}, portal,
                               sleep=sleep, should_stop=lambda: clock.stop)
    assert result.stopped == "Stopped by you." and len(saved) == 1
    assert len(excel_store.read_frame(excel_store.account_file(tmp_path, CA))) > 0


def test_history_stops_at_meters_first_reading_and_remembers_it(settings, tmp_path, small_batches):
    portal = CountingPortal(make_days(date(2026, 8, 15), date(2026, 10, 29)))
    result, new, _ = run_all({**settings, "bootstrap_start": "2026-01-01"}, portal, sleep=StopAfter().sleep)
    assert result.first == "2026-08-15 00:00" and not result.more
    assert new["accounts"][CA]["history_floor"] == "2026-08-15"
    again = CountingPortal([])
    run(new, again)
    assert again.windows == [] and again.logins == 0       # nothing left to ask TNB for


class CountingPortal(FakePortal):
    """Counts browser opens; raises TnbThrottled on chosen fetch calls (int = once, callable = rule)."""

    def __init__(self, store, throttle_on_call=None):
        super().__init__(store)
        self.entered = self.exited = self.calls = 0
        rule = throttle_on_call
        self.throttle = rule if callable(rule) else (lambda n: n == rule)

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.exited += 1

    def fetch_rows(self, sdpudcid, start, end):
        self.calls += 1
        if self.throttle(self.calls):
            raise TnbThrottled("TNB may be limiting")
        return super().fetch_rows(sdpudcid, start, end)
