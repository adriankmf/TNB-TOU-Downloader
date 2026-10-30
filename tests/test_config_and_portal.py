import json
from datetime import date

import pytest

import config
from tnb_portal import classify_url, find_sdpudcid, parse_account_items

LEGACY = {
    "cookie_string": "A=B", "saved_session_cookie": "x", "password": "secret-value",
    "username": "me@example.com", "sdpudcid": "40000000000000000001",
    "excel_path": r"D:\Data\TNB Calculator\tnb_usage.xlsx", "bootstrap_start_date": "2025-03-01",
    "smartmeter_url": "https://bad_253D", "api_base_url": "https://x", "request_timeout": 30,
}


def test_migrate_legacy_drops_secrets_and_maps_fields():
    s = config.migrate_legacy(LEGACY)
    assert "password" not in s and "cookie_string" not in s and "smartmeter_url" not in s
    assert s["email"] == "me@example.com"
    assert s["excel_folder"].endswith("TNB Calculator")
    assert s["legacy_excel"] == LEGACY["excel_path"]
    assert s["bootstrap_start"] == "2025-03-01"
    assert s["accounts"] == {}  # old meter ID is dropped; it is re-detected from the portal
    assert s["selected_account"] == "" and s["browser"] == "chrome"


def test_old_2024_default_becomes_auto():
    assert config.DEFAULTS["bootstrap_start"] == ""
    assert config.migrate_legacy({"bootstrap_start": "2024-01-01"})["bootstrap_start"] == ""
    assert config.migrate_legacy({"bootstrap_start": "2025-06-01"})["bootstrap_start"] == "2025-06-01"


def test_default_bootstrap_start_is_two_months_back():
    assert config.default_bootstrap_start(date(2026, 10, 30)) == "2026-08-31"


def test_first_run_setup_not_done_by_default():
    assert config.DEFAULTS["setup_done"] is False
    assert config.migrate_legacy({"setup_done": True})["setup_done"] is True


@pytest.mark.parametrize("value, expected", [
    ("", ""),                     # empty -> auto
    ("2026-08-31", ""),           # left at the shown default -> stays auto
    ("2025-06-01", "2025-06-01"),
])
def test_parse_bootstrap_start(value, expected):
    assert config.parse_bootstrap_start(value, date(2026, 10, 30)) == expected


@pytest.mark.parametrize("value", ["01/06/2025", "2026-11-01"])
def test_parse_bootstrap_start_rejects_bad_or_future(value):
    with pytest.raises(ValueError):
        config.parse_bootstrap_start(value, date(2026, 10, 30))


def test_selenium_usage_stats_are_off():
    import os
    import tnb_portal  # noqa: F401 -- importing it sets the variable
    assert os.environ.get("SE_AVOID_STATS") == "true"


def test_load_settings_rewrites_file_without_password(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(LEGACY), encoding="utf-8")
    s = config.load_settings(path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "secret-value" not in path.read_text(encoding="utf-8")
    assert on_disk["email"] == s["email"] == "me@example.com"
    # second load is stable and keeps the migration marker until the app clears it
    assert config.load_settings(path)["legacy_excel"] == LEGACY["excel_path"]


def test_load_settings_survives_corrupt_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert config.load_settings(path)["selected_account"] == ""  # no built-in account number


def test_with_account_returns_copy():
    s = config.migrate_legacy({})
    s2 = config.with_account(s, "1", name="Farm", guid="g", sdpudcid="")
    assert s["accounts"] == {}
    assert s2["accounts"]["1"] == {"name": "Farm", "guid": "g"}
    s3 = config.with_account(s2, "1", sdpudcid="99")
    assert s3["accounts"]["1"] == {"name": "Farm", "guid": "g", "sdpudcid": "99"}


def test_parse_account_items_dedupes_and_splits_name():
    items = [
        {"id": "bbbb", "text": "Farm - \n            210000000002"},
        {"id": "aaaa", "text": "Home - \n      220000000001"},
        {"id": "cccc", "text": "Shop - \n   210000000003"},
        {"id": "bbbb", "text": "Farm - \n                    210000000002"},
        {"id": "x", "text": "Add account"},
    ]
    assert parse_account_items(items) == [
        {"ca_no": "210000000002", "name": "Farm", "guid": "bbbb"},
        {"ca_no": "220000000001", "name": "Home", "guid": "aaaa"},
        {"ca_no": "210000000003", "name": "Shop", "guid": "cccc"},
    ]


def test_find_sdpudcid():
    assert find_sdpudcid('x {"sdpudcid":"40000000000000000001","a":1}') == "40000000000000000001"
    assert find_sdpudcid('"sdpudcid" : "123456"') == "123456"
    assert find_sdpudcid("nothing") is None and find_sdpudcid(None) is None


def test_classify_url():
    assert classify_url("https://myaccount.mytnb.com.my/AccountManagement/IndividualDashboard") == "dashboard"
    assert classify_url("https://myaccount.mytnb.com.my/Home/SessionExpired") == "expired"
    assert classify_url("https://www.mytnb.com.my/") == "login"
    assert classify_url("https://smartliving.myaccount.mytnb.com.my/dashboard") == "smartliving"
    assert classify_url("https://smartliving.myaccount.mytnb.com.my/login") == "smartliving_login"
    assert classify_url("about:blank") == "other"


def test_usage_fetch_sends_the_ajax_headers_tnb_requires():
    # Without these TNB answers HTTP 500 with an HTML page (seen live 2026-10-29).
    from tnb_portal import _FETCH_HEADERS
    assert _FETCH_HEADERS["X-Request"] == "JSON"
    assert _FETCH_HEADERS["X-Requested-With"] == "XMLHttpRequest"


def test_describe_data_reply_spots_login_redirect_and_errors():
    from tnb_portal import describe_data_reply
    good = {"data": {"timeseries": [{"data": [{"datetime": "2026-08-13+00:00", "value": 0.5}]}]}}
    assert describe_data_reply(200, json.dumps(good)) == "ok"
    assert describe_data_reply(200, '{"data": {"timeseries": []}}') == "reply has no readings"
    assert describe_data_reply(200, '{"data": {"timeseries": [{"data": []}]}}') == "reply has no readings"
    assert describe_data_reply(200, '{"data": [], "redirect": true, "redirectTo": "/login"}') == "redirect to /login"
    assert describe_data_reply(200, "<br /><b>Fatal error</b>").startswith("not JSON")
    assert describe_data_reply(500, "<html><title>Oops</title></html>").startswith("HTTP 500")
    assert describe_data_reply(0, "TypeError: Failed to fetch").startswith("HTTP 0")


def test_password_is_kept_on_this_pc_only(monkeypatch):
    import credentials

    class FakeVault:  # stands in for keyring's WinVaultKeyring
        persist = "enterprise"  # keyring's default roams with a domain profile

        def set_password(self, service, user, pw):
            self.saved = (service, user, pw, self.persist)

    vault = FakeVault()
    monkeypatch.setattr(credentials.keyring, "get_keyring", lambda: vault)
    credentials.set_password("me@example.com", "pw")
    assert vault.saved == (credentials.SERVICE, "me@example.com", "pw", credentials.LOCAL_MACHINE)
