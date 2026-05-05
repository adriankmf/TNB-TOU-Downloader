"""
tnb_auth.py
-----------
Authentication for the myTNB Smart Living portal.

STRATEGY  (two tiers, tried in order)
--------------------------------------
1. Username / Password login (preferred — automatic):
   POST credentials to the myTNB account portal, follow the SSO redirect
   chain, and land on the smartliving portal with a valid session.

2. Manual session cookie (fallback):
   Read TNB_COOKIE_STRING from config (set via the GUI Settings tab).
   The cookie is a direct copy of the browser's Cookie request header.

How to use credentials
----------------------
  Enter your myTNB username (email) and password in the Settings tab.
  The app will log in automatically on every sync.

How to refresh the cookie (fallback only)
-----------------------------------------
  1. Open Chrome -> https://smartliving.myaccount.mytnb.com.my
  2. Press F12 -> Network tab -> click any request -> Headers
  3. Copy the full "Cookie:" request header value
  4. Paste it into the app Settings tab and click Save Settings

Public API
----------
  get_session()        -> cloudscraper.CloudScraper  (authenticated)
  invalidate_session() -> None
"""

import logging
import re
from typing import Optional
from urllib.parse import urlparse

import cloudscraper

import config

logger = logging.getLogger(__name__)

USAGE_PAGE_PATH      = "/commodity/electric/usage"
ACCOUNT_BASE_URL     = "https://myaccount.mytnb.com.my"
_SMARTLIVING_DOMAINS = [
    "smartliving.myaccount.mytnb.com.my",
]

_session: Optional[cloudscraper.CloudScraper] = None


# -- Public interface -----------------------------------------------------------

def get_session() -> cloudscraper.CloudScraper:
    global _session
    if _session is None:
        _session = _create_authenticated_session()
    return _session


def invalidate_session() -> None:
    global _session
    _session = None
    logger.info("Session cleared -- will re-authenticate on next request.")


# -- Session creation -----------------------------------------------------------

def _create_authenticated_session() -> cloudscraper.CloudScraper:
    # Tier 1: username / password
    if config.TNB_USERNAME and config.TNB_PASSWORD:
        logger.info("Attempting login with username/password…")
        return _login_with_credentials(config.TNB_USERNAME, config.TNB_PASSWORD)

    # Tier 2: manual cookie
    if config.TNB_COOKIE_STRING:
        logger.info("No credentials configured — using session cookie.")
        return _login_with_cookie(config.TNB_COOKIE_STRING)

    raise ValueError(
        "No credentials or cookie configured.\n\n"
        "Go to the Settings tab and enter your myTNB username & password\n"
        "(or paste a session cookie), then click Save Settings."
    )

# -- Credential login ----------------------------------------------------------

def _login_with_credentials(username: str, password: str) -> cloudscraper.CloudScraper:
    """
    Log in with username and password via the myTNB account portal.
    Follows the full SSO redirect chain and returns an authenticated session.
    """
    session = _new_scraper()

    login_url = ACCOUNT_BASE_URL + config.TNB_LOGIN_PATH

    # Step 1 — GET login page to pick up CSRF token and cookies
    try:
        resp = session.get(login_url, timeout=config.REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        raise ValueError(f"Could not reach login page: {exc}") from exc

    csrf_token = _extract_csrf_token(resp.text)
    if not csrf_token:
        logger.warning("CSRF token not found on login page — proceeding without it.")

    # Step 2 — POST credentials
    post_data = {
        "Username": username,
        "Password": password,
    }
    if csrf_token:
        post_data["__RequestVerificationToken"] = csrf_token

    try:
        resp = session.post(
            login_url,
            data=post_data,
            timeout=config.REQUEST_TIMEOUT,
            allow_redirects=True,
        )
    except Exception as exc:
        raise ValueError(f"Login POST failed: {exc}") from exc

    # Step 3 — Navigate to the smartliving usage page (completes SSO chain)
    target = config.TNB_API_BASE_URL.rstrip("/") + USAGE_PAGE_PATH
    try:
        resp = session.get(target, timeout=config.REQUEST_TIMEOUT, allow_redirects=True)
    except Exception as exc:
        raise ValueError(f"Could not reach portal after login: {exc}") from exc

    if not _on_smartliving(resp.url):
        # Check if we landed back on the login page (wrong credentials)
        if "login" in resp.url.lower() or "user/login" in resp.url.lower():
            raise ValueError(
                "Login failed — username or password is incorrect.\n\n"
                "Please check your credentials in the Settings tab."
            )
        raise ValueError(
            f"Login did not reach the portal (landed on: {resp.url}).\n\n"
            "This may be a temporary portal issue — please try again."
        )

    logger.info("[OK] Logged in with username/password.")
    return session


def _extract_csrf_token(html: str) -> str:
    """Extract ASP.NET anti-forgery token from an HTML login form."""
    match = re.search(
        r'<input[^>]+name="__RequestVerificationToken"[^>]+value="([^"]+)"',
        html, re.IGNORECASE,
    )
    if match:
        return match.group(1)
    # Also try the hidden input with the token in a different attribute order
    match = re.search(
        r'<input[^>]+value="([^"]+)"[^>]+name="__RequestVerificationToken"',
        html, re.IGNORECASE,
    )
    return match.group(1) if match else ""


# -- Cookie login --------------------------------------------------------------

def _login_with_cookie(cookie_str: str) -> cloudscraper.CloudScraper:
    session = _new_scraper()
    _load_cookie_string(session, cookie_str)

    if _session_is_valid(session):
        logger.info("[OK] Cookie valid -- authenticated.")
        return session

    raise ValueError(
        "Cookie has expired or is invalid.\n\n"
        "Please either:\n"
        "  • Enter your username & password in the Settings tab, OR\n"
        "  • Refresh the cookie:\n"
        "      1. Open Chrome -> myTNB Smart Living portal\n"
        "      2. F12 -> Network tab -> click any request -> Headers\n"
        "      3. Copy the full Cookie: value\n"
        "      4. Paste it into the Settings tab and click Save Settings."
    )


def _new_scraper() -> cloudscraper.CloudScraper:
    session = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    session.headers.update({"Accept-Language": "en-US,en;q=0.9"})
    return session


def _load_cookie_string(session: cloudscraper.CloudScraper, cookie_str: str) -> None:
    """Parse 'name=val; name=val' cookie header and inject into the session."""
    domain = urlparse(config.TNB_API_BASE_URL).netloc
    count = 0
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" in pair:
            name, _, value = pair.partition("=")
            session.cookies.set(name.strip(), value.strip(), domain=domain)
            count += 1
    logger.debug("Injected %d cookie(s) for domain '%s'.", count, domain)


# -- Session validation ---------------------------------------------------------

def _session_is_valid(session: cloudscraper.CloudScraper) -> bool:
    """Return True if a request to the protected page stays on the smartliving domain
    and does NOT land on the login page (which is also on the same domain)."""
    protected_url = config.TNB_API_BASE_URL.rstrip("/") + USAGE_PAGE_PATH
    try:
        resp = session.get(
            protected_url,
            timeout=config.REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        final_url = resp.url.lower()
        # Must be on smartliving domain
        if not _on_smartliving(resp.url):
            logger.debug("Session validation: redirected off smartliving domain → %s", resp.url)
            return False
        # Must NOT have landed on the login page (same domain, different path)
        if "/login" in final_url:
            logger.debug("Session validation: redirected to login page → %s", resp.url)
            return False
        return True
    except Exception as exc:
        logger.debug("Session validation failed: %s", exc)
        return False


def _on_smartliving(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(d in host for d in _SMARTLIVING_DOMAINS)
