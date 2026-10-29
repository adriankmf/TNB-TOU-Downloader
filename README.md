# TNB Smart Sync

Windows desktop app that downloads your 30-minute TOU smart-meter readings from the myTNB
**Smart Living** portal into Excel (one file per account) and shows them on a built-in dashboard.

Version 2.0. See [CHANGELOG.md](CHANGELOG.md) for what changed.

> Personal tool, not affiliated with or endorsed by TNB. It uses your own myTNB login and reads
> the same data you see on the myTNB website.

- Own login screen (myTNB email + password). The password is saved in **Windows Credential
  Manager** (entry "TNB Smart Sync") on this PC only, never in a file.
- Logs in by itself with a **headless** Chrome (Edge as fallback). You never see a browser,
  unless TNB asks for a CAPTCHA/OTP. Then a window opens so you can finish it.
- Account picker for all accounts on the login (e.g. Home, Farm, Shop). The first account found is selected until you pick another.
- Incremental sync from the last saved reading. No duplicates, and every day is checked for 48 readings.
- Dashboard: daily peak/off-peak bars, peak share, average load profile (weekday vs weekend),
  latest full day, monthly totals.

## Set up

Needs Windows 10/11, Python 3.11 or newer (tick "Add python.exe to PATH" when installing), and
Google Chrome or Microsoft Edge.

```bat
git clone https://github.com/adriankmf/TNB-TOU-Downloader.git
cd TNB-TOU-Downloader
"Start TNB Sync.bat"
```

The first start installs the packages from `requirements.txt` by itself.

## Use it

| What | How |
|---|---|
| Open the app | Double-click **Start TNB Sync.bat** (or `dist\TNB_TOU.exe`) |
| First time | Sign in with your myTNB email + password, then press **Sync now** |
| Sync without the window | **Quick Sync.bat** (syncs the account selected in the app) |
| Re-try incomplete days | Click **Fill gaps** in the app, or `python main.py --fill-gaps` |
| Other account from CLI | `python main.py --account 210000000002` / `python main.py --list` |
| Build the exe | **Build EXE.bat** (runs the tests first; keeps `dist\settings.json`) |

Settings (gear icon): Excel folder, browser (Chrome/Edge), first download date for new accounts, sign out.

## Excel files

`<Excel folder>\tnb_usage_<account no>.xlsx`, sheet **Usage**:

| Timestamp | kWh | RatePlan | Category |
|---|---|---|---|
| 2026-10-27 14:00:00 | 0.612 | TNB NEM Domestic TOU | peak_energy_charge |

- `Category` is the TOU period from TNB. Rows from before the TOU tariff have it empty, and the
  dashboard then estimates the split with the TOU rule: peak = Mon–Fri 2 PM–10 PM, public holidays off-peak.
- The first run moves the old single `tnb_usage.xlsx` into the selected account's file. It removes the duplicate
  12-Aug-2026 rows and keeps a byte-for-byte backup `tnb_usage_backup_<date>.xlsx` next to it.
- Close the file in Excel before syncing. Otherwise the app says so and writes nothing.

## How it works

```
app.py (pywebview window, ui/)  ─┐
main.py (command line)          ─┴─> sync.py ──> tnb_portal.py   headless Chrome, profile browser_profile\
                                          │        login → account → "View Energy Usage" → Smart Living
                                          │        → fetch('/my_energy_request/timeseries') inside the page
                                          ├────> tnb_data.py     parse, ≤30-day windows, 48-points/day check
                                          └────> excel_store.py  per-account workbook, watermark, gaps
usage_stats.py  dashboard numbers      config.py  settings.json      credentials.py  keyring
```

Portal facts (checked live, Oct 2026):

- Accounts: `li.accountListItem` on `/AccountManagement/IndividualDashboard` (id = account GUID).
  The account's "View Energy Usage" link is a stable SmartMeter hand-off URL. It is cached per
  account in `settings.json`, together with the meter's `sdpudcid`.
- `/timeseries` gives complete data for windows of up to 30 days. A **77-day request silently
  returns 1 day**, so the app uses ≤30-day windows. If a window comes back short, the app asks
  again for just the missing days in half-size windows. A day that is still incomplete is
  reported, never skipped silently.
- The myTNB session expires after some idle time and usually does not survive a browser restart, so
  each sync normally logs in again by itself with the saved password (headless, a few seconds).
- During myTNB's scheduled maintenance the website disables Login. The app detects the maintenance
  banner and says so instead of failing silently. Try again after the time shown.

## Files

| File | Purpose |
|---|---|
| `settings.json` | email, Excel folder, selected account, account cache (no password) |
| `browser_profile\` | the background Chrome's profile (cookies/cache; the login itself comes from the saved password) |
| `tnb_sync.log` | detailed log ("Open log" in Settings) |
| `tests\` | offline tests: `python -m pytest -q tests` |

`settings.json`, `browser_profile\`, the log and all Excel files are in `.gitignore`, so your
email, account numbers and readings never end up in git.

## Password and privacy

- The password is kept in Windows Credential Manager (Control Panel → Credential Manager →
  Windows Credentials → "TNB Smart Sync"), saved as "local machine". It stays on this PC: it does
  not roam with a Windows domain profile, and if you copy the app folder to another PC, that PC
  asks for the password once.
- It is saved only after myTNB accepts the login. **Settings → Sign out** deletes it.
- Your login and readings go only to myTNB (the dashboard downloads the Chart.js library from
  cdnjs, nothing more). No account number, meter ID or email is built into the code.

## Troubleshooting

- **"browser profile is busy"**: another Chrome window is using `browser_profile\`. Close it and try again.
- **"myTNB is under maintenance"**: TNB's website is down for maintenance. Sync again later.
- **Login fails**: check the password on the myTNB website, then sign in again (Settings → Sign out).
- **Charts empty**: Chart.js loads from the internet (cdnjs). The data in Excel is not affected.
