# TNB TOU Downloader

A Windows desktop tool that automatically downloads your **30-minute interval electricity usage data** from the myTNB Smart Living portal and saves it to Excel — then lets you visualise and analyse it in a rich browser-based dashboard.

---

## Changelog

### v1.1 — May 2026

- **Bug fix: hardcoded Meter ID removed** — the app previously fell back to a hardcoded `sdpudcid` value when the cookie didn't contain one, causing syncs to silently fetch another account's data. The app now **requires** a valid Meter ID and shows a clear guide if it can't be auto-detected.
- **Improved Meter ID detection** — `_extract_sdpudcid` now scans all cookies (not just `e3LoginReturn`), handles single and double URL-encoding, and uses a regex to reliably find the 20-digit meter number anywhere in a cookie value.
- **Session expiry detection** — after 5 consecutive redirected days, sync now fails fast with a clear "cookie expired" error instead of silently returning empty data.
- **Improved session validation** — a cookie that redirects to the login page (same domain, `/login` path) is now correctly rejected as invalid.
- **Better redirect logging** — HTTP status code is now included in redirect debug messages.

---

## What it Does

### TNB Smart Sync (the app)

`TNB_TOU.exe` is a GUI application that:

- **Logs in automatically** to the myTNB Smart Living portal using your myTNB username and password (or a session cookie as fallback)
- **Downloads your 30-minute interval usage data** from the `/my_energy_request/timeseries` API endpoint
- **Saves the data to an Excel file** (`tnb_usage.xlsx`) with three columns: `Timestamp`, `kWh`, `RatePlan`
- **Incremental sync** — on each run it only fetches data newer than the last saved record (watermark), so syncing is fast after the first run
- **Deduplicates** — never writes the same interval twice, even if you sync overlapping date ranges
- **Bootstrap** — on first run, fetches historical data back to a start date you configure (default: 30 days back)

![TNB Smart Sync Screenshot](https://i.imgur.com/placeholder.png)

#### Tabs in the App

| Tab | Purpose |
|-----|---------|
| **Cookie & Settings** | Enter your myTNB username/password (or paste a session cookie as fallback). Set your meter ID, history start date, and Excel output path. |
| **Sync Data** | Click **Sync Now** to download new data. Live progress log shows each step. |
| **Data Preview** | Scrollable table showing the last 500 rows of your saved Excel data. |

---

### TNB TOU Dashboard (the dashboard)

`TNB_TOU_Dashboard.html` is a fully offline, browser-based analytics dashboard. It runs entirely in your browser — no server, no internet required after opening.

#### How to Load Your Data

1. Run a sync in the app to generate/update `tnb_usage.xlsx`
2. Open `TNB_TOU_Dashboard.html` in any modern browser (Chrome, Edge, Firefox)
3. On the upload screen, **drag and drop** your `tnb_usage.xlsx` file onto the drop zone — or click to browse and select it
4. The dashboard loads and analyses your data instantly

> You need to reload the file each time you want to see fresh data after a sync.

#### Dashboard Tabs

**📊 Tariff Analysis**
- Compares your actual cost under the **TNB TOU (Time-of-Use)** tariff vs. the old flat-rate tariff
- Shows savings (or extra cost) from being on TOU pricing
- Summary cards: Total kWh, TOU cost, Old tariff cost, Net saving/extra
- Monthly breakdown table with per-month savings
- Editable tariff rate inputs — change any rate and click **Recalculate** to instantly see updated figures
- Donut chart: Peak vs. Off-Peak vs. Non-TOU consumption split

**📈 Load Profile**
- Line/bar chart of your electricity usage over time
- Granularity selector: 30-min intervals, hourly, daily, weekly, monthly
- Filters by date range with quick buttons (Last 7 days, 30 days, 3 months, This year, All)
- Overlays Peak / Off-Peak / Non-TOU period colouring on the chart
- Average daily load curve (what your typical usage looks like hour by hour)

**🗺️ Heatmap**
- 2D heatmap: rows = hour of day, columns = day of week (or date)
- Instantly reveals your usage patterns — when you consume the most electricity
- Colour intensity scales from low (green) to high (red)

**☀️ Solar**
- Simulates the impact of adding rooftop solar to your home
- Enter your system size (kW) and daily generation estimate
- Shows estimated self-consumption, grid export, and revised electricity bill

---

## Requirements

- Windows 10 / 11
- A myTNB account with Smart Meter enrolled in Time-of-Use (TOU) pricing
- Internet connection (for syncing data from the portal)

---

## Quick Start

### Option A — Use the Executable (Recommended)

1. Download `TNB_TOU.exe` from the [Releases](https://github.com/adriankmf/TNB-TOU-Downloader/releases) page
2. Place it in any folder (it writes `settings.json` and `tnb_usage.xlsx` next to itself)
3. Double-click `TNB_TOU.exe`
4. Go to **Cookie & Settings** tab → enter your myTNB username and password → click **Save & Test Connection**
5. Go to **Sync Data** tab → click **Sync Now**
6. Open `TNB_TOU_Dashboard.html` in your browser and load the generated `tnb_usage.xlsx`

### Option B — Run from Source

```bash
# Clone the repo
git clone https://github.com/adriankmf/TNB-TOU-Downloader.git
cd TNB-TOU-Downloader

# Install dependencies
pip install -r requirements.txt

# Run the GUI
python app.py

# Or run headless (command-line sync only)
python main.py
```

### Option C — Build the Exe Yourself

```bash
pip install -r requirements.txt
build.bat
# Output: dist\TNB_TOU.exe
```

---

## Configuration

All settings are saved to `settings.json` (auto-created next to the exe). This file is **gitignored** and never uploaded — it contains your credentials.

| Setting | Description |
|---------|-------------|
| `username` | Your myTNB portal email |
| `password` | Your myTNB portal password |
| `cookie_string` | Session cookie (optional fallback if password login fails) |
| `sdpudcid` | Your smart meter ID — auto-detected from cookie. If blank, find it manually (see below) |
| `bootstrap_start_date` | Date to fetch history from on first run (YYYY-MM-DD) |
| `excel_path` | Full path to the output Excel file |

---

## Finding Your Meter ID (sdpudcid)

The Meter ID is a 20-digit number that identifies your smart meter. The app detects it automatically from your session cookie. If it cannot be found:

1. Open Chrome and log into the myTNB Smart Living portal
2. Navigate to the **Usage** page: `https://smartliving.myaccount.mytnb.com.my/commodity/electric/usage`
3. Press **F12** → **Network** tab
4. Look for a request named **`timeseries`** in the list
5. Click it → **Headers** tab → scroll to **Request URL**
6. Find `sdpudcid=` in the URL — copy the number after it
7. Paste it into the **Meter ID** field in the app Settings tab

---

## Authentication

The app supports two login methods (tried in order):

1. **Username + Password** *(recommended)* — logs in automatically every sync via the myTNB portal login page
2. **Session Cookie** *(fallback)* — paste a cookie copied from your browser's DevTools; expires after a few hours of inactivity

---

## Excel File Schema

Sheet: `Usage`

| Column | Type | Example |
|--------|------|---------|
| `Timestamp` | datetime | `2026-04-11 00:00` |
| `kWh` | float | `0.57` |
| `RatePlan` | string | `TNB NEM Domestic General` |

One row = one 30-minute interval.

---

## Security

- Credentials are stored only in `settings.json` on your local machine
- `settings.json` is in `.gitignore` and is never committed or pushed to GitHub
- The dashboard is fully offline — your data never leaves your computer

---

## Project Structure

```
TNB-TOU-Downloader/
├── app.py                  # GUI application (tkinter)
├── main.py                 # Headless CLI sync script
├── tnb_auth.py             # Login (username/password + cookie fallback)
├── tnb_api.py              # myTNB API calls & data parsing
├── excel_handler.py        # Excel read/write/watermark/deduplication
├── config.py               # Configuration loader
├── TNB_TOU_Dashboard.html  # Browser-based analytics dashboard
├── TNB Usage/
│   └── TNB_TOU_Dashboard.html
├── requirements.txt        # Python dependencies
├── TNB_TOU.spec            # PyInstaller build spec
├── build.bat               # One-click build script
└── TNB_TOU.bat             # Launch CLI sync via double-click
```

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `cloudscraper` | HTTP client with Cloudflare bypass |
| `requests` | HTTP library |
| `pandas` | Data manipulation |
| `openpyxl` | Excel read/write |
| `pyinstaller` | Build standalone exe |

---

## License

MIT
