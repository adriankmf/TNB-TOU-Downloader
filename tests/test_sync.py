from datetime import date, datetime

import pytest

import config
import excel_store
import sync
from conftest import make_day, make_days
from tnb_data import ServerError
from tnb_portal import SessionExpired

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
    result, _ = run(settings, portal)
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


def test_dead_session_gives_clear_error_and_keeps_fetched_rows(settings, tmp_path):
    # 1 Sep..28 Oct = two windows; the first arrives, then the session never comes back.
    portal = FlakyPortal(make_days(date(2026, 9, 1), date(2026, 10, 29)), fails=lambda n: n > 1)
    with pytest.raises(SessionExpired, match="keeps logging out"):
        run(settings, portal)
    saved = excel_store.read_frame(excel_store.account_file(tmp_path, CA))
    assert len(saved) == 30 * 48
    assert portal.recovered == 1 and portal.opened == 2


def test_sync_never_logs_in_again_more_than_once(settings, tmp_path):
    # Every first and second try fails, so each window would need a full re-login.
    portal = FlakyPortal(make_days(date(2026, 5, 1), date(2026, 10, 29)), fails=lambda n: n % 3 != 0)
    with pytest.raises(SessionExpired, match="keeps logging out"):
        run({**settings, "bootstrap_start": "2026-05-01"}, portal)
    assert portal.opened == 1 + sync.MAX_RELOGINS
    assert portal.logins == 1 + sync.MAX_RELOGINS


def test_flapping_session_stops_after_recovery_limit(settings, tmp_path):
    # Every window's first try fails: each costs one recovery until the limit is used up.
    portal = FlakyPortal(make_days(date(2026, 5, 1), date(2026, 10, 29)), fails=lambda n: n % 2 == 1)
    with pytest.raises(SessionExpired, match="keeps logging out"):
        run({**settings, "bootstrap_start": "2026-05-01"}, portal)
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
