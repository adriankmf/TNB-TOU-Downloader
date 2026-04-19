# Software Requirements Document (SRD)
## TNB TOU Downloader — v1.0
**Date:** April 2026  
**Purpose:** Full technical specification of the existing system, intended as the reference baseline for building a Chrome Extension companion that auto-captures the session cookie.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Module Specifications](#3-module-specifications)
4. [myTNB Portal API Reference](#4-mytnb-portal-api-reference)
5. [Authentication Flows](#5-authentication-flows)
6. [Data Storage](#6-data-storage)
7. [Settings Contract](#7-settings-contract)
8. [Chrome Extension Integration Requirements](#8-chrome-extension-integration-requirements)
9. [Known Behaviours & Edge Cases](#9-known-behaviours--edge-cases)
10. [Glossary](#10-glossary)

---

## 1. System Overview

TNB TOU Downloader is a Windows desktop application that authenticates to the **myTNB Smart Living portal** and downloads the user's 30-minute interval electricity usage data, saving it to an Excel file for offline analysis via a companion HTML dashboard.

### Components

| Component | File | Type | Purpose |
|-----------|------|------|---------|
| GUI Application | `app.py` | Python / tkinter | Main desktop UI |
| Headless Sync | `main.py` | Python CLI | Background sync without GUI |
| Authentication | `tnb_auth.py` | Python module | Login & session management |
| API Client | `tnb_api.py` | Python module | HTTP calls to myTNB API |
| Data Layer | `excel_handler.py` | Python module | Excel read/write/watermark |
| Configuration | `config.py` | Python module | Settings loader & live reload |
| Dashboard | `TNB_TOU_Dashboard.html` | HTML/JS (offline) | Data visualisation |
| Settings Store | `settings.json` | JSON file | Persisted user configuration |

---

## 2. Architecture

```
┌─────────────────────────────────────────────────┐
│                 GUI (app.py)                    │
│  ┌──────────────┐  ┌──────────┐  ┌──────────┐  │
│  │ Settings Tab │  │ Sync Tab │  │ Data Tab │  │
│  └──────┬───────┘  └────┬─────┘  └────┬─────┘  │
└─────────┼───────────────┼─────────────┼─────────┘
          │               │             │
          ▼               ▼             ▼
    config.py ◄──── settings.json   excel_handler.py
          │
          ▼
    tnb_auth.py
     (2-tier login)
          │
          ▼
    tnb_api.py ──► myTNB Portal API
          │        smartliving.myaccount.mytnb.com.my
          ▼
    excel_handler.py ──► tnb_usage.xlsx
          │
          ▼
    TNB_TOU_Dashboard.html (browser, load xlsx manually)
```

### Data Flow

1. User configures credentials in `settings.json` (via GUI or direct edit)
2. `config.py` loads settings at startup; `config.reload()` applies live changes
3. `tnb_auth.get_session()` returns an authenticated `cloudscraper` session
4. `tnb_api.get_usage_dataframe()` fetches data day-by-day from the portal
5. `excel_handler.append_new_rows()` deduplicates and writes to Excel
6. User opens `TNB_TOU_Dashboard.html` in browser and drops the Excel file onto it

---

## 3. Module Specifications

### 3.1 `config.py`

**Responsibility:** Load and expose all runtime configuration. Provide a `reload()` method for live updates without restart.

**Settings file location:**
- When running as `.py`: same directory as `config.py`
- When running as PyInstaller `.exe`: same directory as the `.exe`

**Public variables:**

| Variable | Type | Default | Source Key |
|----------|------|---------|------------|
| `TNB_COOKIE_STRING` | `str` | `""` | `cookie_string` |
| `TNB_USERNAME` | `str` | `""` | `username` |
| `TNB_PASSWORD` | `str` | `""` | `password` |
| `TNB_SDPUDCID` | `str` | `""` | `sdpudcid` |
| `TNB_API_BASE_URL` | `str` | `https://smartliving.myaccount.mytnb.com.my` | `api_base_url` |
| `TNB_LOGIN_PATH` | `str` | `/User/Login` | hardcoded |
| `TNB_ORG_NAME` | `str` | `TNB` | hardcoded |
| `TNB_TIMEZONE` | `str` | `Asia/Kuala_Lumpur` | hardcoded |
| `TNB_DATEFMT_API` | `str` | `yyyy-MM-dd HH:mm` | hardcoded |
| `TNB_DATEFMT_PY` | `str` | `%Y-%m-%d %H:%M` | hardcoded |
| `TNB_GRANULARITY` | `str` | `MIN30` | hardcoded |
| `EXCEL_FILE_PATH` | `Path` | `./tnb_usage.xlsx` | `excel_path` |
| `BOOTSTRAP_START_DATE` | `datetime \| None` | `None` | `bootstrap_start_date` |
| `BOOTSTRAP_DAYS` | `int` | `30` | `bootstrap_days` |
| `REQUEST_TIMEOUT` | `int` | `30` | `request_timeout` |

**Public functions:**

```python
def reload(new_settings: dict) -> None
```
Persists `new_settings` to `settings.json` and immediately updates all module-level variables. Called by the GUI after the user saves settings.

---

### 3.2 `tnb_auth.py`

**Responsibility:** Provide a single authenticated `cloudscraper.CloudScraper` session. Manages a module-level singleton session.

**Public functions:**

```python
def get_session() -> cloudscraper.CloudScraper
```
Returns the cached session, creating it if not yet initialised. Tries Tier 1 first, falls back to Tier 2.

```python
def invalidate_session() -> None
```
Clears the cached session. Must be called after settings change so the next `get_session()` creates a fresh authenticated session.

**Authentication tiers (in priority order):**

| Tier | Condition | Method |
|------|-----------|--------|
| 1 | `TNB_USERNAME` and `TNB_PASSWORD` are both non-empty | POST to `/User/Login` with CSRF token, follow SSO redirects |
| 2 | `TNB_COOKIE_STRING` is non-empty | Inject raw cookie header into session, validate against protected page |

**Session validation:**  
A session is considered valid if a GET to `{TNB_API_BASE_URL}/commodity/electric/usage` resolves (after redirects) to a URL whose host contains `smartliving.myaccount.mytnb.com.my`.

**Scraper configuration:**
```python
cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "mobile": False}
)
# + Accept-Language: en-US,en;q=0.9
```

---

### 3.3 `tnb_api.py`

**Responsibility:** Fetch 30-minute interval usage data from the portal's timeseries endpoint.

**Public functions:**

```python
def get_usage_dataframe(start_dt: datetime, end_dt: datetime) -> pd.DataFrame
```
Returns a DataFrame with columns `[Timestamp, kWh, RatePlan]`. Iterates day-by-day; each day is one API request.

**Internal call sequence:**

1. `_warm_up_session()` — GET `/commodity/electric/usage` with `Accept: text/html` to set portal-side cookies
2. Loop per calendar day: `_fetch_day(day_start, day_end)`
3. Each `_fetch_day` calls `_build_url()` → GET request → parse JSON → `_flatten_response()`

**Important behavioural notes (confirmed from live API):**

- **Must visit usage page before timeseries** — without warm-up, API returns `{"redirect": true}` 
- **Soft redirect handling** — if API returns `{"redirect": true, "redirectTo": "/login"}` as HTTP 200, re-warm and retry once
- **Hard redirect (302) = no data** — treat as empty day, do not follow
- **`allow_redirects=False`** on timeseries requests — must not auto-follow 302

**DataFrame output schema:**

| Column | dtype | Example |
|--------|-------|---------|
| `Timestamp` | `datetime64[ns]` | `2026-04-11 00:00:00` |
| `kWh` | `float64` | `0.57` |
| `RatePlan` | `str` | `TNB NEM Domestic General` |

---

### 3.4 `excel_handler.py`

**Responsibility:** All read/write operations on the Excel database.

**Public functions:**

```python
def get_start_datetime() -> datetime
```
Returns the fetch start point:
- No file → `bootstrap_start_date` or `(now - BOOTSTRAP_DAYS)`
- File exists → `last_timestamp + 30min`

```python
def append_new_rows(new_df: pd.DataFrame) -> int
```
- Loads all existing timestamps into a set
- Filters `new_df` to only rows not already in the file
- If file doesn't exist: creates it with `pandas.ExcelWriter`
- If file exists: appends row-by-row using `openpyxl` (preserves charts/formatting)
- Returns count of rows actually written

**Excel file schema:**

```
Sheet: "Usage"
Columns: Timestamp (datetime) | kWh (float) | RatePlan (string)
```

---

### 3.5 `app.py` (GUI)

**Tabs:**

| Tab | Key widgets | Behaviour |
|-----|-------------|-----------|
| Cookie & Settings | Username entry, Password entry (masked), Cookie text area (5 rows), Meter ID, Start Date, Excel path | `Save & Test Connection` → `config.reload()` + `tnb_auth.invalidate_session()` + background auth test |
| Sync Data | Sync Now button, progress bar, scrolled log | Spawns background thread → `excel_handler.get_start_datetime()` → `tnb_api.get_usage_dataframe()` → `excel_handler.append_new_rows()` |
| Data Preview | Treeview (last 500 rows) | Reads Excel on tab focus via `pandas.read_excel` |

**Settings priority in sync:** GUI reads current field values each time Sync is clicked — does not require Save first.

**Meter ID auto-detection:** Extracts `sdpudcid` from `e3LoginReturn` cookie value (double URL-decoded).

---

## 4. myTNB Portal API Reference

### 4.1 Base URLs

| Purpose | URL |
|---------|-----|
| Account / Login | `https://myaccount.mytnb.com.my` |
| Smart Living portal | `https://smartliving.myaccount.mytnb.com.my` |

### 4.2 Login Endpoint

```
GET  https://myaccount.mytnb.com.my/User/Login
POST https://myaccount.mytnb.com.my/User/Login
```

**GET** returns an HTML page containing an ASP.NET anti-forgery token:
```html
<input name="__RequestVerificationToken" type="hidden" value="TOKEN_VALUE" />
```

**POST fields:**

| Field | Value |
|-------|-------|
| `Username` | myTNB email address |
| `Password` | myTNB password |
| `__RequestVerificationToken` | CSRF token from GET response |

**Success:** 302 redirect chain ending at `smartliving.myaccount.mytnb.com.my`  
**Failure:** Redirects back to `/User/Login`

### 4.3 Session Warm-Up

```
GET https://smartliving.myaccount.mytnb.com.my/commodity/electric/usage
Accept: text/html,application/xhtml+xml,*/*
```

Must be called before `/my_energy_request/timeseries`. Sets portal-side session cookies required by the controller.

### 4.4 Timeseries Endpoint

```
GET https://smartliving.myaccount.mytnb.com.my/my_energy_request/timeseries
```

**Required headers:**
```
Accept: application/json, text/javascript, */*
X-Requested-With: XMLHttpRequest
Referer: https://smartliving.myaccount.mytnb.com.my/commodity/electric/usage
```

**Query parameters:**

| Parameter | Value | Notes |
|-----------|-------|-------|
| `orgname` | `TNB` | Always |
| `sdpudcid` | `40000000000013274078` | Smart meter ID |
| `dateformat` | `yyyy-MM-dd+HH:mm` | Literal `+`, not space |
| `timezone` | `Asia/Kuala_Lumpur` | Always |
| `view` | `DAY` | Always |
| `granularity` | `MIN30` | 30-minute intervals |
| `start` | `2026-04-11+00:00` | URL-encoded as `%2B` |
| `end` | `2026-04-12+00:00` | Next day midnight |
| `metric` | `usage` | Always |

> **Critical:** The `+` separator between date and time must be literal `+` (encoded as `%2B` in the URL). A space (encoded as `+`) causes the PHP controller to fail with `"Undefined index: view"`.

**Successful response:**
```json
{
  "data": {
    "timeseries": [
      {
        "start": "2026-04-11 00:00",
        "end":   "2026-04-11 00:30",
        "data": [
          {
            "datetime":  "2026-04-11 00:00",
            "value":     0.57,
            "rateplan":  "TNB NEM Domestic General"
          }
        ]
      }
    ]
  }
}
```

**Auth-expired response (HTTP 200, soft redirect):**
```json
{ "redirect": true, "redirectTo": "/login" }
```

**No-data response:** HTTP 302 (no body relevant)

**Error response:**
```json
{ "errorMessage": "...", "errors": [...] }
```

### 4.5 Key Cookies

| Cookie | Domain | Purpose |
|--------|--------|---------|
| `e3AuthToken` | `smartliving.myaccount.mytnb.com.my` | Session authentication token |
| `e3LoginReturn` | `smartliving.myaccount.mytnb.com.my` | Encodes redirect URL containing `sdpudcid` |
| `ssoToken` | `myaccount.mytnb.com.my` | SSO session token |
| `ssoAccount` | `myaccount.mytnb.com.my` | Account identifier |
| `.AspNetCore.Cookies` | `myaccount.mytnb.com.my` | ASP.NET Core auth cookie |
| `__cf_bm` | `.mytnb.com.my` | Cloudflare bot management |
| `_cfuvid` | `.mytnb.com.my` | Cloudflare unique visitor ID |
| `dtCookie` / `dtCookieccd954kf` | portal domains | Dynatrace session tracking |
| `rxVisitorccd954kf` | portal domains | Dynatrace visitor ID |
| `7F9F206065754566847803E7619D23A7` | `myaccount.mytnb.com.my` | JWT token (contains UserId, UserName, RoleIds) |

**Extracting `sdpudcid` from `e3LoginReturn`:**
```python
import urllib.parse

def extract_sdpudcid(cookie_str: str) -> str:
    for part in cookie_str.split(";"):
        part = part.strip()
        if part.lower().startswith("e3loginreturn="):
            val = urllib.parse.unquote(part.split("=", 1)[1])
            val = urllib.parse.unquote(val)   # double-encoded
            if "sdpudcid=" in val:
                return val.split("sdpudcid=")[1].split("&")[0]
    return ""
```

---

## 5. Authentication Flows

### 5.1 Username/Password Login Flow

```
App                         myaccount.mytnb.com.my       smartliving.myaccount.mytnb.com.my
 │                                    │                                │
 │── GET /User/Login ────────────────►│                                │
 │◄── 200 HTML (CSRF token) ─────────│                                │
 │                                    │                                │
 │── POST /User/Login ───────────────►│                                │
 │   Username=...                     │                                │
 │   Password=...                     │                                │
 │   __RequestVerificationToken=...   │                                │
 │◄── 302 → SSO redirect chain ──────│                                │
 │                                    │                                │
 │── GET /commodity/electric/usage ──────────────────────────────────►│
 │   (warm-up — sets portal cookies)                                  │
 │◄── 200 HTML usage page ───────────────────────────────────────────│
 │                                    │                                │
 │── GET /my_energy_request/timeseries?... ──────────────────────────►│
 │◄── 200 JSON timeseries data ──────────────────────────────────────│
```

### 5.2 Cookie Login Flow

```
App                         smartliving.myaccount.mytnb.com.my
 │                                    │
 │  (inject raw cookie string)        │
 │── GET /commodity/electric/usage ──►│
 │◄── 200 HTML (if valid)            │
 │   or 302 to login (if expired)    │
 │                                    │
 │── GET /my_energy_request/timeseries?... ──►│
 │◄── 200 JSON                               │
```

### 5.3 Session Expiry Handling

On soft-redirect from timeseries (`{"redirect": true}`):
1. Call `_warm_up_session()` again
2. Retry the timeseries request once
3. If soft-redirect persists after retry → skip that day, continue

---

## 6. Data Storage

### 6.1 Excel File (`tnb_usage.xlsx`)

- Sheet name: `Usage`
- Sorted chronologically by `Timestamp`
- New rows always appended at the bottom
- Existing rows never modified
- Deduplication by `Timestamp` set membership check

### 6.2 `settings.json`

See [Section 7](#7-settings-contract). Stored next to the exe/script. **Never committed to source control.**

---

## 7. Settings Contract

The `settings.json` file is the shared state between the GUI, the Python sync modules, and the planned Chrome Extension.

**Full schema:**

```json
{
  "username":             "user@example.com",
  "password":             "yourpassword",
  "cookie_string":        "name1=val1; name2=val2; ...",
  "sdpudcid":             "40000000000013274078",
  "api_base_url":         "https://smartliving.myaccount.mytnb.com.my",
  "excel_path":           "C:\\path\\to\\tnb_usage.xlsx",
  "bootstrap_start_date": "2026-01-01",
  "bootstrap_days":       30,
  "request_timeout":      30
}
```

**Field details:**

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| `username` | One of username+password or cookie_string | string | myTNB login email |
| `password` | One of username+password or cookie_string | string | myTNB login password |
| `cookie_string` | One of username+password or cookie_string | string | Full `Cookie:` header value copied from browser |
| `sdpudcid` | Yes | string | Smart meter ID (20-digit number) |
| `api_base_url` | No | string | Portal base URL; default `https://smartliving.myaccount.mytnb.com.my` |
| `excel_path` | No | string | Absolute path to output Excel file |
| `bootstrap_start_date` | No | string `YYYY-MM-DD` | First sync start date |
| `bootstrap_days` | No | integer | Days of history if no start date set |
| `request_timeout` | No | integer | HTTP request timeout in seconds |

---

## 8. Chrome Extension Integration Requirements

This section defines what the Chrome Extension (to be built) must do and how it integrates with the existing desktop app.

### 8.1 Purpose

The Chrome Extension acts as a **cookie bridge**: it detects when the user is authenticated on the myTNB Smart Living portal in their browser, automatically captures the session cookie string, and writes it to `settings.json` so the desktop app can use it without the user manually copying from DevTools.

### 8.2 Target Pages

The extension must be active on:

```
https://smartliving.myaccount.mytnb.com.my/*
https://myaccount.mytnb.com.my/*
```

### 8.3 Cookie Capture Specification

**What to capture:**  
The full `Cookie:` request header value — a semicolon-separated string of all cookies for the domain. This is equivalent to what you see in Chrome DevTools → Network → any request → Request Headers → `Cookie:`.

**How to capture using the Chrome Extensions API:**

```javascript
// In background service worker (Manifest V3)
chrome.cookies.getAll({ domain: "smartliving.myaccount.mytnb.com.my" }, (cookies) => {
  const cookieString = cookies.map(c => `${c.name}=${c.value}`).join("; ");
  // also include myaccount.mytnb.com.my cookies
});

chrome.cookies.getAll({ domain: "myaccount.mytnb.com.my" }, (cookies) => {
  // merge both domains
});
```

**Required Chrome permissions:**
```json
{
  "permissions": ["cookies", "storage", "nativeMessaging"],
  "host_permissions": [
    "https://smartliving.myaccount.mytnb.com.my/*",
    "https://myaccount.mytnb.com.my/*"
  ]
}
```

### 8.4 Trigger Conditions

Capture and push the cookie when **any** of these occur:

1. User navigates to `smartliving.myaccount.mytnb.com.my/commodity/electric/usage`
2. User clicks the extension popup button "Capture Cookie"
3. A tab on the target domain becomes active (optional — on tab focus)

### 8.5 Delivery Method to Desktop App

The Chrome Extension cannot write to the filesystem directly. Options (in recommended order):

#### Option A — Native Messaging (Recommended)

Use Chrome's [Native Messaging API](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging) with a small Python helper (`tnb_native_host.py`) that receives the cookie from the extension and writes it to `settings.json`.

**Flow:**
```
Extension ──► chrome.runtime.sendNativeMessage() ──► tnb_native_host.py
                                                            │
                                                     reads/writes settings.json
                                                            │
                                                     desktop app picks up on next sync
```

**Native host manifest** (registered in Windows Registry):
```json
{
  "name": "com.tnb.cookiebridge",
  "description": "TNB cookie bridge",
  "path": "C:\\path\\to\\tnb_native_host.exe",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://YOUR_EXTENSION_ID/"]
}
```

**Message format (Extension → Native Host):**
```json
{
  "action": "set_cookie",
  "cookie_string": "name1=val1; name2=val2; ...",
  "sdpudcid": "40000000000013274078"
}
```

**Message format (Native Host → Extension):**
```json
{
  "status": "ok",
  "settings_path": "C:\\Users\\...\\tnb_usage.xlsx"
}
```

#### Option B — Local HTTP Server (Simpler to implement)

The desktop app runs a small local HTTP server (e.g. on `http://localhost:57842`). The extension POSTs the cookie to it.

**Extension POST:**
```javascript
fetch("http://localhost:57842/update-cookie", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    cookie_string: cookieStr,
    sdpudcid: extractedSdpudcid
  })
});
```

**Desktop app endpoint** (Python, e.g. using `http.server`):
- Receives POST
- Merges into current `settings.json`
- Calls `config.reload()`
- Calls `tnb_auth.invalidate_session()`

> **Security note:** Bind only to `127.0.0.1`, not `0.0.0.0`. Add a shared secret token in the POST body that matches a value in `settings.json` to prevent other local processes from injecting cookies.

#### Option C — Shared File Polling

Extension writes cookie to a known temp file (e.g. `tnb_cookie_update.json` in `%TEMP%`). Desktop app polls this file every N seconds and merges it.

> Least reliable. Only use if native messaging is impractical.

### 8.6 Extension UI Requirements

**Popup (popup.html):**

| Element | Behaviour |
|---------|-----------|
| Status indicator | Shows "Authenticated" (green) / "Not authenticated" (red) based on whether `smartliving.myaccount.mytnb.com.my` cookies are present |
| Meter ID display | Shows extracted `sdpudcid` from `e3LoginReturn` cookie |
| "Capture & Send Cookie" button | Triggers cookie capture → delivery to desktop app |
| Cookie age indicator | Shows approximate session age based on `rxvtccd954kf` cookie timestamp |
| "Open Portal" link | Opens `https://smartliving.myaccount.mytnb.com.my/commodity/electric/usage` in a new tab |

### 8.7 `sdpudcid` Extraction (Extension)

```javascript
function extractSdpudcid(cookies) {
  const e3 = cookies.find(c => c.name === "e3LoginReturn");
  if (!e3) return null;
  try {
    const decoded = decodeURIComponent(decodeURIComponent(e3.value));
    const match = decoded.match(/sdpudcid=([^&]+)/);
    return match ? match[1] : null;
  } catch {
    return null;
  }
}
```

### 8.8 Auto-Trigger on Login Detection

Detect login completion by monitoring for the presence of the `e3AuthToken` cookie on `smartliving.myaccount.mytnb.com.my`:

```javascript
chrome.cookies.onChanged.addListener((changeInfo) => {
  if (
    !changeInfo.removed &&
    changeInfo.cookie.name === "e3AuthToken" &&
    changeInfo.cookie.domain.includes("smartliving.myaccount.mytnb.com.my")
  ) {
    // User just logged in — auto-capture
    captureCookieAndSend();
  }
});
```

### 8.9 `settings.json` Update Contract

When the extension writes to `settings.json` (via native host or HTTP), it must:

1. Read the existing `settings.json` first (to preserve `username`, `password`, `excel_path`, etc.)
2. Update only `cookie_string` and `sdpudcid`
3. Write back the full merged object
4. **Never overwrite** `username`, `password`, `excel_path`, `bootstrap_start_date`, `bootstrap_days`, `request_timeout`

```python
# tnb_native_host.py — merge logic
with open(settings_path) as f:
    existing = json.load(f)

existing["cookie_string"] = msg["cookie_string"]
if msg.get("sdpudcid"):
    existing["sdpudcid"] = msg["sdpudcid"]

with open(settings_path, "w") as f:
    json.dump(existing, f, indent=2)
```

---

## 9. Known Behaviours & Edge Cases

| # | Behaviour | Notes |
|---|-----------|-------|
| 1 | **Timeseries requires warm-up** | GET usage page first; without it API returns `{"redirect":true}` even with valid session |
| 2 | **datetime `+` separator is literal** | PHP controller fails if `+` is decoded to space; must be `%2B` in URL |
| 3 | **One API request per calendar day** | API does not support multi-day ranges; loop day by day |
| 4 | **302 redirect = no data** | Not an error; TNB has not published data for that day yet |
| 5 | **Soft redirect retry** | Retry once with warm-up on `{"redirect":true}`; skip day if it persists |
| 6 | **Cookie domain scope** | `_load_cookie_string` sets cookies for `smartliving.myaccount.mytnb.com.my` only; login-related cookies on `myaccount.mytnb.com.my` may also be needed for full validation |
| 7 | **Session validation check** | Only `smartliving.myaccount.mytnb.com.my` in the final URL is valid — `myaccount.mytnb.com.my` (login domain) is explicitly excluded |
| 8 | **Cloudflare** | `cloudscraper` with `chrome/windows` browser profile handles CF JS challenges |
| 9 | **Double URL-encoding of `e3LoginReturn`** | Must call `urllib.parse.unquote()` twice to extract `sdpudcid` |
| 10 | **Cookie expiry** | No exact TTL documented; observed sessions last a few hours of inactivity |

---

## 10. Glossary

| Term | Definition |
|------|------------|
| **sdpudcid** | Smart meter identifier (20-digit string). Uniquely identifies the user's meter on the TNB system |
| **TOU** | Time-of-Use — a tariff structure where electricity price varies by time of day (Peak / Off-Peak) |
| **Timeseries endpoint** | `/my_energy_request/timeseries` — the API endpoint returning 30-min interval usage |
| **Warm-up** | GET request to the usage page before the timeseries call, required to set portal-side session state |
| **Watermark** | The last `Timestamp` value stored in the Excel file, used to determine the start of the next sync |
| **Bootstrap** | First-run logic: fetch historical data back to `bootstrap_start_date` or `now - bootstrap_days` |
| **Soft redirect** | `{"redirect": true}` returned as HTTP 200 JSON from the timeseries API when session is invalid |
| **Cookie string** | The full value of the `Cookie:` HTTP request header (semicolon-separated name=value pairs) |
| **Native Messaging** | Chrome Extension API allowing communication with a locally installed executable |
| **CSRF token** | `__RequestVerificationToken` — ASP.NET anti-forgery token required in the login POST |
| **SSO** | Single Sign-On — the redirect chain from `myaccount.mytnb.com.my` to `smartliving.myaccount.mytnb.com.my` after login |
