"""
tnb_portal.py
-------------
Drives myTNB in a real (headless) Chrome so the user never has to open a browser.

Flow (confirmed live 2026-10-28):
  1. https://myaccount.mytnb.com.my/AccountManagement/IndividualDashboard
     - logged in  -> lists accounts as li.accountListItem (id = account GUID, text "Name - caNo")
     - logged out -> redirects to the www.mytnb.com.my login form (#frm-login: #Email, #Password)
  2. Clicking an account li selects it; its "View Energy Usage" link is the SmartMeter hand-off URL
     (stable per account), which SSOs into smartliving.myaccount.mytnb.com.my/dashboard.
  3. The Smart Living page source carries "sdpudcid":"<meter id>".
  4. /my_energy_request/timeseries is fetched with fetch() from inside that tab, so the browser's
     own cookies are used -- nothing is copied out of the browser.

The persistent profile (browser_profile/) is tried first, but the myTNB session usually does not
survive a browser restart, so most syncs log in again headlessly with the saved password.
A visible window opens only if TNB asks for a CAPTCHA/OTP.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import JavascriptException, SessionNotCreatedException, WebDriverException

from tnb_data import ServerError, UsageRow, build_timeseries_path, parse_timeseries

logger = logging.getLogger(__name__)

DASHBOARD_URL = "https://myaccount.mytnb.com.my/AccountManagement/IndividualDashboard"
LOGIN_URL = "https://www.mytnb.com.my/"
SMARTLIVING_HOST = "smartliving.myaccount.mytnb.com.my"

LOGIN_TIMEOUT_S = 45
VISIBLE_LOGIN_TIMEOUT_S = 300
PAGE_TIMEOUT_S = 45
SMARTLIVING_LOGIN_SETTLE_S = 10
SMARTLIVING_URL_SETTLE_S = 5
SMARTLIVING_USAGE_PATH = "/commodity/electric/usage"
DATA_READY_TIMEOUT_S = 40
DATA_READY_POLL_S = 3
DATA_READY_OK_STREAK = 2
FETCH_TIMEOUT_S = 120
LAUNCH_RETRIES = 3

_SDPUDCID_RE = re.compile(r'"sdpudcid"\s*:\s*"(\d+)"')
_ACCOUNT_TEXT_RE = re.compile(r"^(.*?)\s*-\s*(\d{6,})\s*$", re.S)

Log = Callable[[str], None]


class PortalError(RuntimeError):
    """Something on the myTNB side did not behave as expected."""


class LoginError(PortalError):
    """Login failed (wrong password, or the user did not finish verification)."""


class SessionExpired(PortalError):
    """The Smart Living tab is no longer logged in."""


class BrowserBusyError(PortalError):
    """The app's browser profile is in use by another window."""


# -- Pure helpers (unit-tested) -----------------------------------------------------------------

def parse_account_items(items: list[dict]) -> list[dict]:
    """[{id, text}] from li.accountListItem -> unique [{ca_no, name, guid}] in page order."""
    accounts, seen = [], set()
    for item in items:
        match = _ACCOUNT_TEXT_RE.match(" ".join(str(item.get("text", "")).split()))
        if not match or match.group(2) in seen:
            continue
        seen.add(match.group(2))
        accounts.append({"ca_no": match.group(2), "name": match.group(1).strip(), "guid": item.get("id", "")})
    return accounts


def _brief(body: str) -> str:
    """A short, readable error body: an HTML error page is reduced to its title."""
    title = re.search(r"<title>(.*?)</title>", body or "", re.S | re.I)
    if title:
        return "web page: " + " ".join(title.group(1).split())[:120]
    return "" if "<html" in (body or "").lower() else (body or "")[:200]


def find_sdpudcid(page_source: str) -> str | None:
    match = _SDPUDCID_RE.search(page_source or "")
    return match.group(1) if match else None


def describe_data_reply(status: object, body: str) -> str:
    """'ok' if a Smart Living data reply carries real data, else a short reason (for logs)."""
    if status != 200:
        return f"HTTP {status} {_brief(body)[:60]}".strip()
    try:
        data = json.loads(body)
    except ValueError:
        return "not JSON: " + (_brief(body) or body[:60])
    if not isinstance(data, dict):
        return "unexpected reply"
    if data.get("redirect"):
        return f"redirect to {data.get('redirectTo')}"
    if not isinstance(data.get("data"), dict):
        return "no data in reply"
    # A half-ready session answers valid JSON with no points (seen live 2026-10-29), and the
    # app then saved 0 rows without any error -- only a reply with readings counts.
    slots = data["data"].get("timeseries") or []
    has_points = any(isinstance(s, dict) and s.get("data") for s in slots)
    return "ok" if has_points else "reply has no readings"


def classify_url(url: str) -> str:
    """'dashboard' | 'smartliving' | 'smartliving_login' | 'expired' | 'login' | 'other'."""
    parsed = urlparse(url or "")
    host, path = parsed.netloc.lower(), parsed.path.lower()
    if host == SMARTLIVING_HOST:
        return "smartliving_login" if path.startswith("/login") else "smartliving"
    if host == "myaccount.mytnb.com.my":
        if "sessionexpired" in path or "/account/login" in path:
            return "expired"
        return "dashboard"
    if host.endswith("mytnb.com.my"):
        return "login"
    return "other"


# -- Browser session ----------------------------------------------------------------------------

# Same headers the Smart Living page sends. Without X-Request/X-Requested-With the server
# renders its HTML page instead of JSON and fails with HTTP 500 (confirmed live 2026-10-29).
_FETCH_HEADERS = {"Accept": "application/json", "X-Request": "JSON", "X-Requested-With": "XMLHttpRequest"}

_FETCH_JS = """
const [url, headers] = arguments, done = arguments[arguments.length - 1];
fetch(url, {credentials: 'include', headers})
  .then(r => r.text().then(t => done({status: r.status, url: r.url, body: t})))
  .catch(e => done({status: 0, url: url, body: String(e)}));
"""

_FILL_LOGIN_JS = """
const [email, password] = arguments;
const form = document.querySelector('#frm-login') || document.querySelector('#Email')?.closest('form');
if (!form) return 'no-form';
const set = (el, v) => {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  setter.call(el, v);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
};
set(form.querySelector('#Email'), email);
set(form.querySelector('#Password'), password);
const btn = [...form.querySelectorAll('button, input[type=submit]')]
  .find(b => /login/i.test(b.innerText || b.value)) || form.querySelector('[type=submit]');
if (!btn) return 'no-button';
if (password) btn.click();
return 'ok';
"""

_LOGIN_STATE_JS = """
const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
const challenge =
  [...document.querySelectorAll('iframe')].some(f => /captcha|turnstile|challenge/i.test(f.src || '') && visible(f)) ||
  [...document.querySelectorAll('input')].some(i => /(^|[^a-z])(otp|tac)([^a-z]|$)|otpcode|taccode/i.test((i.id || '') + ' ' + (i.name || '')) && visible(i));
const form = document.querySelector('#frm-login');
const errors = form ? [...form.querySelectorAll('.field-validation-error, .validation-summary-errors, .error, .alert, .text-danger, [role=alert]')]
  .filter(visible).map(e => e.innerText.trim()).filter(Boolean) : [];
return {challenge, errors, hasForm: !!form};
"""

# myTNB shows this banner and disables the Login button during scheduled maintenance.
_MAINTENANCE_JS = """
const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
const banner = [...document.querySelectorAll('.alert-maintenance.in-maintenance')].find(visible);
const btn = document.querySelector('#frm-login [type=submit]');
const blocked = !!btn && btn.classList.contains('maintenance-disabled');
if (!banner && !blocked) return null;
return (banner ? banner.innerText.trim().split('\\n')[0] : '') || 'Login is disabled on the website';
"""


class TnbPortal:
    """Context manager around one browser run: `with TnbPortal(...) as portal: ...`."""

    def __init__(self, profile_dir: Path, browser: str = "chrome", *, headless: bool = True,
                 log: Log | None = None, attach: str | None = None) -> None:
        self.profile_dir = Path(profile_dir)
        self.browser = browser
        self.headless = headless
        self.log = log or (lambda msg: logger.info(msg))
        # "127.0.0.1:9222" = drive an already-open Chrome (started with --remote-debugging-port)
        # instead of launching one. Used for testing; that Chrome is left open afterwards.
        self.attach = attach
        self.driver = None

    # -- lifecycle --

    def __enter__(self) -> "TnbPortal":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        if self.attach:
            options = webdriver.ChromeOptions()
            options.debugger_address = self.attach
            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(PAGE_TIMEOUT_S)
            self.driver.set_script_timeout(FETCH_TIMEOUT_S)
            self.headless = False
            logger.info("Attached to running Chrome at %s.", self.attach)
            return
        order =[self.browser] + [b for b in ("chrome", "edge") if b != self.browser]
        errors = []
        for name in order:
            try:
                self.driver = self._launch(name)
                self.browser = name
                return
            except BrowserBusyError:
                raise
            except WebDriverException as exc:
                errors.append(f"{name}: {exc.msg or exc}")
                logger.warning("Could not start %s: %s", name, exc)
        raise PortalError("Could not start Chrome or Edge.\n" + "\n".join(errors))

    def close(self) -> None:
        if self.driver is not None:
            try:
                if self.attach:
                    self.driver.service.stop()  # detach only; keep the user's Chrome open
                else:
                    self.driver.quit()
            except WebDriverException:
                pass
            self.driver = None

    def _launch(self, name: str):
        profile = self.profile_dir if name == "chrome" else self.profile_dir.with_name(self.profile_dir.name + "_edge")
        profile.mkdir(parents=True, exist_ok=True)
        if name == "chrome":
            options, factory = webdriver.ChromeOptions(), webdriver.Chrome
        else:
            options, factory = webdriver.EdgeOptions(), webdriver.Edge
        options.add_argument(f"--user-data-dir={profile}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1280,900")
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        if self.headless:
            options.add_argument("--headless=new")

        for attempt in range(1, LAUNCH_RETRIES + 1):
            try:
                driver = factory(options=options)
                break
            except SessionNotCreatedException as exc:
                # A previous run may still be releasing the profile lock.
                if attempt == LAUNCH_RETRIES:
                    raise BrowserBusyError(
                        "The app's browser profile is busy. Close any Chrome window opened by "
                        "TNB Smart Sync (or wait a few seconds) and try again.") from exc
                time.sleep(3)

        driver.set_page_load_timeout(PAGE_TIMEOUT_S)
        driver.set_script_timeout(FETCH_TIMEOUT_S)
        if self.headless:
            # Headless Chrome announces itself in the user agent; present as normal Chrome.
            agent = driver.execute_script("return navigator.userAgent").replace("HeadlessChrome", "Chrome")
            driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": agent})
        logger.info("Started %s (%s).", name, "headless" if self.headless else "visible")
        return driver

    # -- navigation helpers --

    def _get(self, url: str) -> None:
        try:
            self.driver.get(url)
        except WebDriverException as exc:
            # Page-load timeouts are common on myTNB's heavy pages; the DOM is usually usable.
            logger.debug("Navigation to %s: %s", url, exc.msg)

    def _wait(self, condition: Callable[[], object], timeout: float, step: float = 0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = condition()
            except (JavascriptException, WebDriverException):
                value = None
            if value:
                return value
            time.sleep(step)
        return None

    def _state(self) -> str:
        return classify_url(self.driver.current_url)

    def _account_items(self) -> list[dict]:
        return self.driver.execute_script(
            "return [...document.querySelectorAll('li.accountListItem')]"
            ".map(li => ({id: li.id, text: li.innerText}))") or []

    # -- login --

    def ensure_login(self, email: str, password: str | None) -> str:
        """Reach the account dashboard. Returns 'session' (saved session reused) or 'login'."""
        self.log("Opening myTNB...")
        self._get(DASHBOARD_URL)
        reached = self._wait(lambda: "dashboard" if self._account_items()
                             else ("login" if self._state() in ("login", "expired") else None), PAGE_TIMEOUT_S)
        if reached == "dashboard":
            self.log("Saved session still valid.")
            return "session"
        if not email or not password:
            raise LoginError("Not logged in to myTNB -- enter your email and password.")

        self.log("Logging in to myTNB...")
        self._submit_login(email, password)
        outcome = self._wait(self._login_outcome, LOGIN_TIMEOUT_S, step=1)
        if outcome == "ok":
            self.log("Logged in.")
            return "login"
        if isinstance(outcome, str) and outcome.startswith("error:"):
            raise LoginError(outcome[6:])
        if not self.headless:
            raise LoginError("Login did not complete. Check your email and password.")
        return self._visible_login(email, password)

    def _submit_login(self, email: str, password: str) -> None:
        if self._state() != "login" or not self.driver.execute_script("return !!document.querySelector('#Email')"):
            self._get(LOGIN_URL)
        if not self._wait(lambda: self.driver.execute_script("return !!document.querySelector('#Email')"), PAGE_TIMEOUT_S):
            raise PortalError("myTNB login form not found (site changed or offline).")
        maintenance = self.driver.execute_script(_MAINTENANCE_JS)
        if maintenance:
            raise LoginError(f"myTNB is under maintenance ({maintenance[:160]}). Try again later.")
        result = self.driver.execute_script(_FILL_LOGIN_JS, email, password)
        if result != "ok":
            raise PortalError(f"Could not fill the myTNB login form ({result}).")

    def _login_outcome(self) -> str | None:
        state = self._state()
        if state == "dashboard":
            return "ok"
        if state != "login":
            return None
        info = self.driver.execute_script(_LOGIN_STATE_JS) or {}
        if info.get("challenge"):
            return "challenge"
        if info.get("errors"):
            return "error:" + " ".join(info["errors"])[:300]
        return None

    def _visible_login(self, email: str, password: str) -> str:
        """TNB asked for extra verification: reopen visibly so the user can finish it."""
        self.log("myTNB wants extra verification -- a browser window will open. "
                 "Complete the check there; the app continues by itself.")
        self.close()
        self.headless = False
        self.driver = self._launch(self.browser)
        self._get(LOGIN_URL)
        if self._wait(lambda: self.driver.execute_script("return !!document.querySelector('#Email')"), PAGE_TIMEOUT_S):
            self.driver.execute_script(_FILL_LOGIN_JS, email, "")  # pre-fill email only
        if not self._wait(lambda: self._state() == "dashboard", VISIBLE_LOGIN_TIMEOUT_S, step=1):
            raise LoginError("Login was not completed in the browser window within 5 minutes.")
        self.log("Logged in.")
        return "login"

    # -- accounts --

    def list_accounts(self) -> list[dict]:
        if self._state() != "dashboard" or not self._account_items():
            self._get(DASHBOARD_URL)
        items = self._wait(self._account_items, PAGE_TIMEOUT_S)
        accounts = parse_account_items(items or [])
        if not accounts:
            raise PortalError("No accounts found on the myTNB dashboard.")
        return accounts

    def _energy_link(self) -> str | None:
        return self.driver.execute_script(
            "const a = [...document.querySelectorAll('a')].find(a => /view energy usage/i.test(a.innerText)"
            " && /SmartMeter/i.test(a.href)); return a ? a.href : null;")

    def _select_account(self, guid: str) -> str:
        """Click the account in the dashboard list and return its SmartMeter hand-off link."""
        if self._state() != "dashboard" or not self._account_items():
            self._get(DASHBOARD_URL)
            self._wait(self._account_items, PAGE_TIMEOUT_S)
        before = self._energy_link()
        clicked = self.driver.execute_script(
            "window.__tnbMarker = 1;"
            "const li = [...document.querySelectorAll('li.accountListItem')].find(l => l.id === arguments[0]);"
            "if (!li) return false; li.click(); return true;", guid)
        if not clicked:
            raise PortalError("That account is not on the myTNB dashboard any more.")

        def switched():
            reloaded = not self.driver.execute_script("return window.__tnbMarker === 1")
            link = self._energy_link()
            if link and (link != before or reloaded):
                return link
            return None

        link = self._wait(switched, 20) or self._energy_link()  # unchanged = it was already selected
        if not link:
            raise PortalError("The 'View Energy Usage' link did not appear for this account.")
        return link

    # -- Smart Living --

    def open_smart_living(self, account: dict) -> dict:
        """
        Get the Smart Living tab ready for `account` ({ca_no, guid, smartmeter_url?, sdpudcid?}).
        Returns {"smartmeter_url", "sdpudcid"} for caching.
        """
        # The saved link alone opens a Smart Living page whose data calls answer "redirect to
        # /login" (seen live 2026-10-29). Selecting the account on myTNB first gives a working
        # session, so the saved link is only a fallback for accounts without a GUID.
        cached = account.get("smartmeter_url")
        if cached and not account.get("guid"):
            sdpudcid = self._open_link(cached)
            if sdpudcid and (not account.get("sdpudcid") or sdpudcid == account["sdpudcid"]):
                return {"smartmeter_url": cached, "sdpudcid": sdpudcid}
            self.log("Saved Smart Meter link did not work -- selecting the account again.")
        if not account.get("guid"):
            raise PortalError("Account details missing -- refresh the account list.")

        self.log(f"Selecting account {account.get('ca_no')}...")
        link = self._select_account(account["guid"])
        sdpudcid = self._open_link(link)
        if not sdpudcid:
            raise PortalError("Smart Living did not open for this account (no smart meter?).")
        return {"smartmeter_url": link, "sdpudcid": sdpudcid}

    def _open_link(self, link: str) -> str | None:
        self.log("Opening Smart Living...")
        self._get(link)

        # The hand-off passes through Smart Living's /login URL before it redirects to the
        # dashboard, so a login page only counts once it has stayed put for a while.
        login_since: list[float] = []

        def ready():
            state = self._state()
            if state == "smartliving_login":
                login_since.append(time.monotonic())
                return "login" if login_since[-1] - login_since[0] > SMARTLIVING_LOGIN_SETTLE_S else None
            login_since.clear()
            if state == "smartliving":
                return find_sdpudcid(self.driver.page_source)
            return None

        result = self._wait(ready, PAGE_TIMEOUT_S, step=1)
        if result in (None, "login"):
            logger.warning("Smart Living not ready (%s), stuck at %s", result or "timeout", self.driver.current_url)
            return None
        self._wait_data_session(result)
        return result

    def recover_data_session(self, sdpudcid: str) -> None:
        """Session dropped mid-sync: wait for it to come back without a new login (cheaper,
        and every new hand-off makes the session flap again)."""
        self._wait_data_session(sdpudcid)

    def _wait_data_session(self, sdpudcid: str) -> None:
        """
        The dashboard has an sdpudcid long before its data session works: right after the
        hand-off it re-navigates several times and data calls answer "redirect to /login"
        (seen live 2026-10-29). Let the URL settle, open the usage page like a user would,
        then poll the same BILL-view call that page makes until it returns real data.
        """
        self.log("Waiting for Smart Living to finish loading...")
        self._wait_url_settled()
        probe = f"/my_energy_request/timeseries?metric=usage&view=BILL&sdpudcid={sdpudcid}"
        last = "no reply"
        for attempt in (1, 2):
            self._get(f"https://{SMARTLIVING_HOST}{SMARTLIVING_USAGE_PATH}")
            deadline, ok_streak = time.monotonic() + DATA_READY_TIMEOUT_S, 0
            while time.monotonic() < deadline:
                time.sleep(DATA_READY_POLL_S)
                last = self._probe_data(probe)
                logger.info("Smart Living data check %d: %s", attempt, last)
                # The session flaps for a while after the hand-off: one good reply is not enough.
                ok_streak = ok_streak + 1 if last == "ok" else 0
                if ok_streak >= DATA_READY_OK_STREAK:
                    return
        raise PortalError(f"Smart Living opened but would not hand out usage data ({last}). "
                          "TNB may be limiting repeated logins -- try again in about 30 minutes.")

    def _wait_url_settled(self) -> None:
        seen = {"url": None, "since": time.monotonic()}

        def settled():
            url = self.driver.current_url
            if url != seen["url"]:
                seen.update(url=url, since=time.monotonic())
                return False
            return time.monotonic() - seen["since"] >= SMARTLIVING_URL_SETTLE_S

        self._wait(settled, PAGE_TIMEOUT_S, step=1)

    def _probe_data(self, path: str) -> str:
        """'ok' when the data session answers with real JSON, else a short reason."""
        try:
            if self._state() != "smartliving":
                return f"left Smart Living ({classify_url(self.driver.current_url)})"
            reply = self.driver.execute_async_script(_FETCH_JS, path, _FETCH_HEADERS) or {}
        except WebDriverException as exc:  # includes "renderer busy" timeouts while the page loads
            return f"script error: {exc.msg}"
        return describe_data_reply(reply.get("status"), reply.get("body", ""))

    def fetch_rows(self, sdpudcid: str, start: date, end: date) -> list[UsageRow]:
        """Rows for [start, end) via fetch() inside the Smart Living tab."""
        if self._state() != "smartliving":
            raise SessionExpired("Smart Living tab is not open.")
        path = build_timeseries_path(sdpudcid, start, end)
        reply = self.driver.execute_async_script(_FETCH_JS, path, _FETCH_HEADERS) or {}
        status, body = reply.get("status"), reply.get("body", "")
        if status in (401, 403) or classify_url(reply.get("url", "")) == "smartliving_login":
            raise SessionExpired("Smart Living session expired.")
        if status == 0:
            # "Failed to fetch": the tab navigated away mid-request (e.g. bounced to /login).
            raise SessionExpired(f"Smart Living tab changed during the request ({body[:80]}).")
        if isinstance(status, int) and status >= 500:
            raise ServerError(f"TNB server error HTTP {status} for {start:%d %b %Y}..{end - timedelta(days=1):%d %b %Y}")
        if status != 200:
            raise PortalError(f"Usage request failed (HTTP {status}): {_brief(body)}")
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise SessionExpired(f"Usage request returned a web page instead of data: {body[:120]!r}") from exc
        if isinstance(data, dict) and data.get("redirect"):
            raise SessionExpired(f"Smart Living asked to log in again ({data.get('redirectTo')}).")
        return parse_timeseries(data)
