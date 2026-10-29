# TNB Smart Sync

Personal desktop app that downloads my 30-minute TOU smart-meter readings from the myTNB
**Smart Living** portal into Excel (one file per account) and shows them on a built-in dashboard.

- Own login screen (myTNB email + password). The password is saved in **Windows Credential
  Manager** (entry "TNB Smart Sync"), never in a file.
- Logs in by itself with a **headless** Chrome (Edge as fallback). You never see a browser,
  unless TNB asks for a CAPTCHA/OTP. Then a window opens so you can finish it.
- Account picker for all accounts on the login (e.g. Home, Farm, Shop). The first account found is selected until you pick another.
- Incremental sync from the last saved reading. No duplicates, and every day is checked for 48 readings.
- Dashboard: daily peak/off-peak bars, peak share, average load profile (weekday vs weekend),
  latest full day, monthly totals.

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
| `_old\`, `data\` | archived old versions / Excel backups |

## Troubleshooting

- **"browser profile is busy"**: another Chrome window is using `browser_profile\`. Close it and try again.
- **"myTNB is under maintenance"**: TNB's website is down for maintenance. Sync again later.
- **Login fails**: check the password on the myTNB website, then sign in again (Settings → Sign out).
- **Charts empty**: Chart.js loads from the internet (cdnjs). The data in Excel is not affected.
