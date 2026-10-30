# Changelog

## v2.0.1 — 30 Oct 2026

### New
- **First-run setup screen.** After the first sign-in the app asks where to keep the Excel files and
  which date to download from (default 2 months back) before anything is downloaded. Existing installs
  see it once too. The date can't be in the future.

- **Download years of history safely, batch by batch.** At most 6 months per batch, a 3-minute pause
  between batches (browser kept open, no new login), newest days first, then older history backwards.
  If TNB limits the account anyway, the app waits 35 minutes and carries on. A **Stop** button keeps what
  was downloaded, and the next Sync continues. Picking an earlier date in Settings now adds the older
  history to an existing file too. CLI: one batch per run, or `--all`.
- Setup screen: 2 months / 1 / 2 / 3 years buttons with a time estimate.

### Fixed
- **Writing big Excel files is much faster.** Each appended row used to rescan the whole sheet.
- **First download now starts 2 months back** instead of 1 Jan 2024. Pulling ~2 years in one go hit
  TNB's throttle and blocked the account for about 30 minutes. Installs that still had the old
  2024-01-01 default are switched to the new one; a date you picked yourself is kept.
  (The 30 days / 90 days buttons only change the dashboard view, not what is downloaded.)
- **No more usage statistics to plausible.io.** Selenium Manager (the helper that finds chromedriver)
  sent anonymous stats by default; the app now turns that off (`SE_AVOID_STATS=true`).
- Usage requests to TNB are now spaced at least 5 seconds apart.

## v2.0 — 29 Oct 2026

A rewrite. The app now logs in to myTNB by itself in a hidden browser, so you no longer paste cookies.

### New
- **New app window** (pywebview) with its own login screen, an account picker, settings and a
  built-in dashboard: daily peak/off-peak bars, peak share, average load profile (weekday vs
  weekend), latest full day, monthly totals.
- **Logs in by itself** with a headless Chrome (Edge as fallback) and your saved password. If TNB asks
  for a CAPTCHA/OTP, a browser window opens so you can finish it.
- **All accounts on the login**: you pick the account in the app, and each one gets its own Excel file
  `tnb_usage_<account no>.xlsx`.
- **New `Category` column** with the TOU period from TNB (peak / off-peak).
- **Fill gaps**: downloads incomplete days in the history again.
- **Quick Sync.bat / `main.py`**: sync from the command line without opening the window
  (`--account`, `--list`, `--fill-gaps`, `--folder`).
- **Build EXE.bat** runs the tests before it builds `dist\TNB_TOU.exe`.
- Offline test suite (`python -m pytest -q tests`).

### Reliability
- Downloads in windows of at most 30 days. TNB quietly returns only 1 day for longer requests. A window
  that comes back short is asked again in smaller pieces.
- Every day is checked for 48 readings. A day that is still incomplete is reported, not skipped silently.
- Incremental sync from the last saved reading, with no duplicates.
- Detects an expired session and logs in again by itself.
- Detects myTNB's maintenance banner and says so instead of failing silently.
- If the Excel file is open, the app says so and writes nothing.

### Security and privacy
- The password is saved in **Windows Credential Manager**, never in a file. It is saved as
  "local machine", so it stays on this PC: it does not roam with a Windows domain profile, and an app
  folder copied to another PC asks for the password again.
- Old `settings.json` files are cleaned on first start. The plaintext password and old session
  cookies are removed from disk.
- No account number or meter ID is built into the app. The first account found at sign-in is selected.

### Migration from v1.x
- The old single `tnb_usage.xlsx` is moved into the selected account's file on first run, and a
  byte-for-byte backup `tnb_usage_backup_<date>.xlsx` is kept next to it.

### Removed
- Cookie login and the cloudscraper/requests client (`tnb_auth.py`, `tnb_api.py`).
- The separate `TNB_TOU_Dashboard.html` (the dashboard is now inside the app).
- The duplicate `TNB Usage\` folder.

## v1.1 — 6 May 2026

- **Bug fix: hardcoded meter ID removed.** The app fell back to a hardcoded `sdpudcid` when the
  cookie had none, so a sync could quietly fetch another account's data. It now requires a valid
  meter ID and shows a guide if it cannot be detected.
- Better meter ID detection: scans all cookies, handles single and double URL-encoding.
- Session expiry: after 5 redirected days in a row, sync stops with a clear "cookie expired" error.
- A cookie that redirects to the login page is now rejected as invalid.
- HTTP status code added to redirect log messages.

## v1.0 — 17 Apr 2026

- First version: Tkinter app that logs in with username/password (session cookie as fallback),
  downloads 30-minute readings into `tnb_usage.xlsx` with incremental sync, plus an offline HTML
  dashboard.
