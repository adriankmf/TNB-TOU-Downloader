"""
app.py
------
TNB Smart Sync -- GUI Application
Double-click TNB_TOU.exe (or run: python app.py) to launch.

Features
--------
  - Paste your browser session cookie -> Save -> Sync
  - Step-by-step guide for getting the cookie embedded in the UI
  - Background sync with live progress log
  - Data preview table (last 500 rows)
  - Auto-exports to Excel after every sync
  - Settings persisted to settings.json next to the exe
"""

import json
import logging
import os
import queue
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

# ---------------------------------------------------------------------------
# App directory -- works both as .py and as PyInstaller .exe
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

SETTINGS_FILE = APP_DIR / "settings.json"

# ---------------------------------------------------------------------------
# Logging -> queue so the GUI can display it
# ---------------------------------------------------------------------------
_log_queue: queue.Queue = queue.Queue()


class _QueueHandler(logging.Handler):
    def emit(self, record):
        try:
            _log_queue.put((record.levelname, self.format(record)))
        except Exception:
            pass


_handler = _QueueHandler()
_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_handler)

# ---------------------------------------------------------------------------
# Lazy import of sync modules (after logging is configured)
# ---------------------------------------------------------------------------
import config          # noqa: E402
import excel_handler   # noqa: E402
import tnb_api         # noqa: E402
import tnb_auth        # noqa: E402


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

_DEFAULTS: dict = {
    "cookie_string":        "",
    "sdpudcid":             "",          # auto-detected from cookie; never hardcoded
    "api_base_url":         "https://smartliving.myaccount.mytnb.com.my",
    "excel_path":           str(APP_DIR / "tnb_usage.xlsx"),
    "bootstrap_start_date": "2024-01-01",
    "bootstrap_days":       30,
    "request_timeout":      30,
}


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            return {**_DEFAULTS, **d}
        except Exception:
            pass
    return dict(_DEFAULTS)


def _save_settings(d: dict) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Colours / fonts
# ---------------------------------------------------------------------------
CLR_HEADER_BG   = "#1565C0"
CLR_HEADER_FG   = "#FFFFFF"
CLR_HEADER_SUB  = "#BBDEFB"
CLR_LOG_BG      = "#1e1e1e"
CLR_LOG_FG      = "#d4d4d4"
CLR_LOG_WARN    = "#FFD700"
CLR_LOG_ERR     = "#FF6B6B"
CLR_LOG_SUCCESS = "#69FF47"
CLR_GUIDE_BG    = "#EEF4FF"
FONT_UI         = ("Segoe UI", 9)
FONT_MONO       = ("Consolas", 8)
FONT_TITLE      = ("Segoe UI", 16, "bold")
FONT_SUBTITLE   = ("Segoe UI", 10)


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class TNBApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("TNB Smart Sync")
        self.geometry("960x720")
        self.minsize(800, 600)
        self.resizable(True, True)

        self.settings = _load_settings()
        self._syncing = False

        self._build_ui()
        self._apply_settings_to_ui()
        self._poll_log_queue()

        # Open Settings tab if no credentials/cookie yet, otherwise Sync tab
        if not self.settings.get("username") and not self.settings.get("cookie_string"):
            self.notebook.select(0)
            self._append_log("INFO",
                "Welcome! Enter your myTNB username & password in the Settings tab to get started.")
        else:
            self.notebook.select(1)

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self):
        # ── Header ──────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=CLR_HEADER_BG, pady=12)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="TNB Smart Sync",
                 font=FONT_TITLE, fg=CLR_HEADER_FG, bg=CLR_HEADER_BG).pack(side=tk.LEFT, padx=16)
        tk.Label(hdr, text="Time-of-Use Energy Data Downloader",
                 font=FONT_SUBTITLE, fg=CLR_HEADER_SUB, bg=CLR_HEADER_BG).pack(side=tk.LEFT)

        # ── Notebook ────────────────────────────────────────────────────────
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self._build_settings_tab()
        self._build_sync_tab()
        self._build_data_tab()

    # ── Tab 1: Cookie & Settings ─────────────────────────────────────────────

    def _build_settings_tab(self):
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text="  Cookie & Settings  ")

        # ── Sticky action bar at the BOTTOM (always visible) ─────────────────
        btn_bar = tk.Frame(outer, pady=8, padx=12, relief="groove", bd=1)
        btn_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self._conn_status_lbl = tk.Label(btn_bar, text="", font=("Segoe UI", 9), anchor="w")
        self._conn_status_lbl.pack(side=tk.LEFT, padx=(0, 12))

        ttk.Button(btn_bar, text="Clear Cookie",
                   command=self._clear_cookie).pack(side=tk.RIGHT)
        ttk.Button(btn_bar, text="Save & Test Connection",
                   command=self._save_and_test).pack(side=tk.RIGHT, padx=6)

        # ── Scrollable content area ───────────────────────────────────────────
        canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tab = ttk.Frame(canvas, padding=12)
        win_id = canvas.create_window((0, 0), window=tab, anchor="nw")

        def _on_resize(e):
            canvas.itemconfig(win_id, width=e.width)
        canvas.bind("<Configure>", _on_resize)

        def _on_frame_resize(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        tab.bind("<Configure>", _on_frame_resize)

        # Mousewheel scroll
        def _on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # ── Collapsible guide ─────────────────────────────────────────────────
        self._guide_open = tk.BooleanVar(value=True)

        guide_hdr = tk.Frame(tab, bg="#1565C0", cursor="hand2")
        guide_hdr.pack(fill=tk.X, pady=(0, 2))
        self._guide_toggle_lbl = tk.Label(
            guide_hdr, text="  ▼  How to get your Session Cookie  (click to hide)",
            font=("Segoe UI", 9, "bold"), fg="white", bg="#1565C0",
            anchor="w", pady=5, padx=6, cursor="hand2")
        self._guide_toggle_lbl.pack(fill=tk.X)
        guide_hdr.bind("<Button-1>", self._toggle_guide)
        self._guide_toggle_lbl.bind("<Button-1>", self._toggle_guide)

        self._guide_frame = tk.Frame(tab, bg=CLR_GUIDE_BG, relief="solid", bd=1)
        self._guide_frame.pack(fill=tk.X, pady=(0, 10))

        steps_text = (
            "Step 1   Open Google Chrome and go to:\n"
            "            https://smartliving.myaccount.mytnb.com.my/commodity/electric/usage\n"
            "            Log in with your myTNB username and password if prompted.\n\n"
            "Step 2   Press  F12  on your keyboard to open Chrome Developer Tools.\n\n"
            "Step 3   Click the  Network  tab at the top of the Developer Tools panel.\n\n"
            "Step 4   Reload the page with  F5 , then click on any request in the list\n"
            "            (e.g. the one named  usage  or  timeseries ).\n\n"
            "Step 5   In the right panel click  Headers , scroll down to  Request Headers\n"
            "            and find the line that starts with  Cookie:\n\n"
            "Step 6   Right-click the Cookie value -> Copy Value  (or click it, Ctrl+A, Ctrl+C).\n\n"
            "Step 7   Paste the copied value into the  Session Cookie  box below,\n"
            "            then click  Save & Test Connection.\n\n"
            "Note     The cookie expires after a few hours of inactivity.\n"
            "            If sync fails with an auth error, repeat steps 1-7 to refresh it."
        )
        tk.Label(self._guide_frame, text=steps_text, justify=tk.LEFT, anchor="nw",
                 font=FONT_UI, bg=CLR_GUIDE_BG, padx=12, pady=8).pack(fill=tk.X)

        # ── Login Credentials ─────────────────────────────────────────────────
        cred_frame = ttk.LabelFrame(tab, text="  Login Credentials  (recommended)  ", padding=10)
        cred_frame.pack(fill=tk.X, pady=(0, 10))

        cg = tk.Frame(cred_frame)
        cg.pack(fill=tk.X)

        tk.Label(cg, text="Username (Email):", font=FONT_UI, anchor="w").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=5)
        self._username_var = tk.StringVar()
        tk.Entry(cg, textvariable=self._username_var, width=36, font=FONT_MONO).grid(
            row=0, column=1, sticky="w", pady=5)

        tk.Label(cg, text="Password:", font=FONT_UI, anchor="w").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=5)
        self._password_var = tk.StringVar()
        self._pw_entry = tk.Entry(cg, textvariable=self._password_var,
                                  width=36, font=FONT_MONO, show="\u2022")
        self._pw_entry.grid(row=1, column=1, sticky="w", pady=5)

        self._show_pw_var = tk.BooleanVar(value=False)
        tk.Checkbutton(cg, text="Show", variable=self._show_pw_var,
                       font=FONT_UI, command=self._toggle_pw_visibility).grid(
            row=1, column=2, sticky="w", padx=8)

        tk.Label(cred_frame,
                 text="Enter your myTNB portal username and password. "
                      "The app will log in automatically on every sync.",
                 font=("Segoe UI", 8, "italic"), fg="#555").pack(anchor="w", pady=(4, 0))

        # ── Cookie input ──────────────────────────────────────────────────────
        ck_frame = ttk.LabelFrame(tab, text="  Session Cookie  (optional fallback)  ", padding=10)
        ck_frame.pack(fill=tk.X, pady=(0, 10))

        self._cookie_text = tk.Text(ck_frame, height=5, font=FONT_MONO,
                                    wrap=tk.WORD, relief="solid", borderwidth=1, undo=True)
        self._cookie_text.pack(fill=tk.X)
        self._cookie_text.bind("<<Modified>>", self._on_cookie_changed)

        tk.Label(ck_frame,
                 text="Optional: paste a cookie here only if you prefer not to save your password. "
                      "Leave empty when using username/password above.",
                 font=("Segoe UI", 8, "italic"), fg="#666").pack(anchor="w", pady=(4, 0))

        # ── Other settings ────────────────────────────────────────────────────
        other = ttk.LabelFrame(tab, text="  Other Settings  ", padding=10)
        other.pack(fill=tk.X, pady=(0, 10))

        grid = tk.Frame(other)
        grid.pack(fill=tk.X)

        def lbl(text, row):
            tk.Label(grid, text=text, font=FONT_UI, anchor="w").grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=6)

        lbl("Meter ID (sdpudcid):", 0)
        self._sdpudcid_var = tk.StringVar()
        tk.Entry(grid, textvariable=self._sdpudcid_var, width=28, font=FONT_MONO).grid(
            row=0, column=1, sticky="w", pady=6)
        meter_hint = tk.Frame(grid)
        meter_hint.grid(row=0, column=2, sticky="w", padx=8)
        tk.Label(meter_hint,
                 text="Auto-detected from cookie  |  If blank: in Chrome, go to the usage page,\n"
                      "F12 → Network → click 'timeseries' → look for sdpudcid= in the Request URL",
                 font=("Segoe UI", 8), fg="#888", justify=tk.LEFT).pack(anchor="w")

        lbl("History Start Date:", 1)
        self._start_date_var = tk.StringVar()
        tk.Entry(grid, textvariable=self._start_date_var, width=14, font=FONT_MONO).grid(
            row=1, column=1, sticky="w", pady=6)
        tk.Label(grid, text="YYYY-MM-DD   (used only on first sync — ignored once Excel file exists)",
                 font=("Segoe UI", 8), fg="#888").grid(row=1, column=2, sticky="w", padx=8)

        lbl("Excel Output File:", 2)
        self._excel_path_var = tk.StringVar()
        pf = tk.Frame(grid)
        pf.grid(row=2, column=1, columnspan=2, sticky="ew", pady=6)
        tk.Entry(pf, textvariable=self._excel_path_var, width=50, font=FONT_MONO).pack(side=tk.LEFT)
        ttk.Button(pf, text="Browse…", command=self._browse_excel).pack(side=tk.LEFT, padx=6)

    def _toggle_guide(self, event=None):
        if self._guide_open.get():
            self._guide_frame.pack_forget()
            self._guide_toggle_lbl.config(
                text="  ▶  How to get your Session Cookie  (click to show)")
            self._guide_open.set(False)
        else:
            # Re-insert guide frame right after the header bar
            self._guide_frame.pack(fill=tk.X, pady=(0, 10))
            self._guide_toggle_lbl.config(
                text="  ▼  How to get your Session Cookie  (click to hide)")
            self._guide_open.set(True)

    # ── Tab 2: Sync ──────────────────────────────────────────────────────────

    def _build_sync_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="  Sync Data  ")

        # Status bar
        status_box = ttk.LabelFrame(tab, text="  Status  ", padding=10)
        status_box.pack(fill=tk.X, pady=(0, 10))

        self._status_lbl = tk.Label(status_box, text="Ready to sync.",
                                    font=("Segoe UI", 10), anchor="w")
        self._status_lbl.pack(fill=tk.X)

        self._progress = ttk.Progressbar(status_box, mode="indeterminate")
        self._progress.pack(fill=tk.X, pady=(6, 0))

        # Action buttons
        btn_row = tk.Frame(tab)
        btn_row.pack(fill=tk.X, pady=(0, 10))

        self._sync_btn = ttk.Button(btn_row, text="  Sync Now  ", command=self._start_sync)
        self._sync_btn.pack(side=tk.LEFT)

        ttk.Button(btn_row, text="Open Excel File", command=self._open_excel).pack(
            side=tk.LEFT, padx=8)

        ttk.Button(btn_row, text="Clear Log", command=self._clear_log).pack(side=tk.RIGHT)

        # Log window
        log_frame = ttk.LabelFrame(tab, text="  Progress Log  ", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self._log_text = scrolledtext.ScrolledText(
            log_frame, font=FONT_MONO, state=tk.DISABLED,
            bg=CLR_LOG_BG, fg=CLR_LOG_FG, relief="flat",
            wrap=tk.WORD, height=20,
        )
        self._log_text.pack(fill=tk.BOTH, expand=True)

        # Log colour tags
        self._log_text.tag_config("INFO",    foreground=CLR_LOG_FG)
        self._log_text.tag_config("WARNING", foreground=CLR_LOG_WARN)
        self._log_text.tag_config("ERROR",   foreground=CLR_LOG_ERR)
        self._log_text.tag_config("SUCCESS", foreground=CLR_LOG_SUCCESS)
        self._log_text.tag_config("DEBUG",   foreground="#888888")

    # ── Tab 3: Data Preview ───────────────────────────────────────────────────

    def _build_data_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="  Data Preview  ")

        # Toolbar
        toolbar = tk.Frame(tab)
        toolbar.pack(fill=tk.X, pady=(0, 8))

        ttk.Button(toolbar, text="Refresh Table", command=self._refresh_table).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Open in Excel", command=self._open_excel).pack(side=tk.LEFT, padx=8)

        self._row_count_lbl = tk.Label(toolbar, text="", font=("Segoe UI", 9), fg="#555")
        self._row_count_lbl.pack(side=tk.RIGHT)

        tk.Label(toolbar, text="Showing last 500 rows  |",
                 font=("Segoe UI", 8, "italic"), fg="#888").pack(side=tk.RIGHT, padx=4)

        # Treeview + scrollbars
        tree_wrap = tk.Frame(tab)
        tree_wrap.pack(fill=tk.BOTH, expand=True)

        cols = ("Timestamp", "kWh", "RatePlan")
        self._tree = ttk.Treeview(tree_wrap, columns=cols, show="headings",
                                   selectmode="browse")

        self._tree.heading("Timestamp", text="Timestamp")
        self._tree.heading("kWh",       text="kWh")
        self._tree.heading("RatePlan",  text="Rate Plan")

        self._tree.column("Timestamp", width=180, anchor="w", stretch=False)
        self._tree.column("kWh",       width=100, anchor="e", stretch=False)
        self._tree.column("RatePlan",  width=400, anchor="w", stretch=True)

        vsb = ttk.Scrollbar(tree_wrap, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(tree_wrap, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)

        self._tree.tag_configure("odd",  background="#F7F7F7")
        self._tree.tag_configure("even", background="#FFFFFF")

        self._refresh_table()

    # -----------------------------------------------------------------------
    # Settings helpers
    # -----------------------------------------------------------------------

    def _toggle_pw_visibility(self):
        self._pw_entry.config(show="" if self._show_pw_var.get() else "\u2022")

    def _apply_settings_to_ui(self):
        self._username_var.set(self.settings.get("username", ""))
        self._password_var.set(self.settings.get("password", ""))
        self._cookie_text.delete("1.0", tk.END)
        self._cookie_text.insert("1.0", self.settings.get("cookie_string", ""))
        self._sdpudcid_var.set(self.settings.get("sdpudcid", ""))
        self._start_date_var.set(self.settings.get("bootstrap_start_date", "2024-01-01"))
        self._excel_path_var.set(self.settings.get("excel_path",
                                                    str(APP_DIR / "tnb_usage.xlsx")))

    def _save_settings(self) -> bool:
        """Save current UI values to settings.json. Returns True on success."""
        username = self._username_var.get().strip()
        password = self._password_var.get().strip()
        cookie   = self._cookie_text.get("1.0", tk.END).strip()

        if not cookie and not (username and password):
            messagebox.showwarning(
                "No Credentials",
                "Please enter both your myTNB username AND password, "
                "or paste a session cookie."
            )
            return False

        sdpudcid = self._sdpudcid_var.get().strip()
        if not sdpudcid and cookie:
            sdpudcid = _extract_sdpudcid(cookie)
            if sdpudcid:
                self._sdpudcid_var.set(sdpudcid)
        if not sdpudcid:
            messagebox.showwarning(
                "Meter ID Required",
                "Could not auto-detect your Meter ID (sdpudcid) from the cookie.\n\n"
                "To find it manually:\n"
                "  1. Open Chrome → TNB Smart Living usage page\n"
                "  2. Press F12 → Network tab\n"
                "  3. Click any 'timeseries' request\n"
                "  4. Look in the Request URL for  sdpudcid=XXXXXXXXXX\n"
                "  5. Copy that number into the Meter ID field above."
            )
            return False

        start_date = self._start_date_var.get().strip()
        if start_date:
            try:
                datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                messagebox.showerror("Invalid Date",
                                     f"History Start Date '{start_date}' is not YYYY-MM-DD.")
                return False

        self.settings = {
            "username":             username,
            "password":             password,
            "cookie_string":        cookie,
            "sdpudcid":             sdpudcid,
            "api_base_url":         "https://smartliving.myaccount.mytnb.com.my",
            "excel_path":           self._excel_path_var.get().strip(),
            "bootstrap_start_date": start_date,
            "bootstrap_days":       30,
            "request_timeout":      30,
        }
        _save_settings(self.settings)
        config.reload(self.settings)
        tnb_auth.invalidate_session()
        return True

    def _save_and_test(self):
        """Save settings then test the connection against the live portal."""
        if not self._save_settings():
            return

        self._conn_status_lbl.config(text="Testing connection…", fg="#555")
        self.update_idletasks()

        def _test():
            try:
                tnb_auth.invalidate_session()
                tnb_auth.get_session()          # raises ValueError if cookie invalid
                _log_queue.put(("__CONN_OK__", ""))
            except Exception as exc:
                _log_queue.put(("__CONN_FAIL__", str(exc)))

        threading.Thread(target=_test, daemon=True).start()

    def _on_cookie_changed(self, event=None):
        """Auto-extract meter ID from cookie whenever the text changes."""
        # Reset the modified flag so this fires again next time
        self._cookie_text.edit_modified(False)
        cookie = self._cookie_text.get("1.0", tk.END).strip()
        if cookie:
            sdpudcid = _extract_sdpudcid(cookie)
            if sdpudcid:
                self._sdpudcid_var.set(sdpudcid)

    def _clear_cookie(self):
        self._cookie_text.delete("1.0", tk.END)

    def _browse_excel(self):
        path = filedialog.asksaveasfilename(
            title="Choose Excel output file",
            defaultextension=".xlsx",
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
            initialfile="tnb_usage.xlsx",
        )
        if path:
            self._excel_path_var.set(path)

    # -----------------------------------------------------------------------
    # Sync
    # -----------------------------------------------------------------------

    def _start_sync(self):
        if self._syncing:
            messagebox.showwarning("Busy", "A sync is already running. Please wait.")
            return

        # Always read the current UI state so the user doesn't have to click Save first
        username = self._username_var.get().strip()
        password = self._password_var.get().strip()
        cookie   = self._cookie_text.get("1.0", tk.END).strip()

        if not cookie and not (username and password):
            messagebox.showwarning(
                "No Credentials",
                "Please enter your myTNB username & password (or session cookie) "
                "in the Settings tab first."
            )
            self.notebook.select(0)
            return

        # Try to extract meter ID from cookie if the field is blank
        sdpudcid = self._sdpudcid_var.get().strip()
        if not sdpudcid and cookie:
            sdpudcid = _extract_sdpudcid(cookie)
            if sdpudcid:
                self._sdpudcid_var.set(sdpudcid)
        if not sdpudcid:
            messagebox.showwarning(
                "Meter ID Required",
                "Meter ID (sdpudcid) is not set — cannot sync without it.\n\n"
                "To find your Meter ID:\n"
                "  1. Open Chrome → TNB Smart Living usage page\n"
                "  2. Press F12 → Network tab\n"
                "  3. Click any 'timeseries' request\n"
                "  4. Look in the Request URL for  sdpudcid=XXXXXXXXXX\n"
                "  5. Copy that number into the Meter ID field in the Settings tab."
            )
            self.notebook.select(0)
            return

        # Auto-save current UI state before syncing
        self.settings.update({
            "username":      username,
            "password":      password,
            "cookie_string": cookie,
            "sdpudcid":      sdpudcid,
            "excel_path":    self._excel_path_var.get().strip() or self.settings.get("excel_path"),
            "bootstrap_start_date": self._start_date_var.get().strip(),
        })
        _save_settings(self.settings)
        config.reload(self.settings)
        tnb_auth.invalidate_session()

        self._syncing = True
        self._sync_btn.config(state=tk.DISABLED, text="  Syncing…  ")
        self._status_lbl.config(text="Syncing — please wait…")
        self._progress.start(10)
        self.notebook.select(1)  # switch to Sync tab

        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self):
        """Runs in a background thread — communicates via _log_queue."""
        try:
            start_dt = excel_handler.get_start_datetime()
            end_dt   = datetime.now()

            if start_dt >= end_dt - timedelta(minutes=30):
                _log_queue.put(("SUCCESS",
                                "Already up to date — no new data available yet. "
                                "Try again after the next 30-minute interval."))
                return

            df = tnb_api.get_usage_dataframe(start_dt, end_dt)

            if df.empty:
                _log_queue.put(("SUCCESS",
                                "The API returned no records for this period. "
                                "This is normal if TNB has not published the data yet."))
                return

            rows_added = excel_handler.append_new_rows(df)
            _log_queue.put(("SUCCESS",
                            f"Done!  Added {rows_added:,} new record(s) to the Excel file."))

        except Exception as exc:
            _log_queue.put(("ERROR", f"Sync failed: {exc}"))
        finally:
            _log_queue.put(("__DONE__", ""))

    def _on_sync_done(self):
        self._syncing = False
        self._sync_btn.config(state=tk.NORMAL, text="  Sync Now  ")
        self._progress.stop()
        self._status_lbl.config(
            text=f"Last synced: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self._refresh_table()

    # -----------------------------------------------------------------------
    # Log helpers
    # -----------------------------------------------------------------------

    def _poll_log_queue(self):
        try:
            while True:
                level, msg = _log_queue.get_nowait()
                if level == "__DONE__":
                    self._on_sync_done()
                elif level == "__CONN_OK__":
                    self._conn_status_lbl.config(
                        text="Connection OK — cookie is valid!", fg="#2E7D32")
                    messagebox.showinfo("Connection OK",
                                        "Cookie is valid and authenticated.\n\n"
                                        "Go to the Sync Data tab and click Sync Now.")
                elif level == "__CONN_FAIL__":
                    self._conn_status_lbl.config(
                        text="Connection FAILED — check your credentials.", fg="#C62828")
                    messagebox.showerror("Connection Failed",
                                         f"Could not authenticate with the portal.\n\n{msg}")
                else:
                    self._append_log(level, msg)
        except queue.Empty:
            pass
        self.after(150, self._poll_log_queue)

    def _append_log(self, level: str, msg: str):
        self._log_text.config(state=tk.NORMAL)
        tag = level if level in ("INFO", "WARNING", "ERROR", "SUCCESS", "DEBUG") else "INFO"
        self._log_text.insert(tk.END, msg + "\n", tag)
        self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _clear_log(self):
        self._log_text.config(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.config(state=tk.DISABLED)

    # -----------------------------------------------------------------------
    # Data table
    # -----------------------------------------------------------------------

    def _refresh_table(self):
        for item in self._tree.get_children():
            self._tree.delete(item)

        excel_path = Path(self.settings.get("excel_path", str(APP_DIR / "tnb_usage.xlsx")))
        if not excel_path.exists():
            self._row_count_lbl.config(text="No Excel file yet — run a sync first.")
            return

        try:
            import pandas as pd
            df = pd.read_excel(excel_path, sheet_name="Usage",
                               parse_dates=["Timestamp"])
            total = len(df)
            preview = df.tail(500)

            for i, (_, row) in enumerate(preview.iterrows()):
                ts  = row["Timestamp"]
                ts  = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
                kwh = f"{row['kWh']:.3f}" if pd.notna(row.get("kWh")) else ""
                rp  = str(row.get("RatePlan", ""))
                tag = "odd" if i % 2 else "even"
                self._tree.insert("", tk.END, values=(ts, kwh, rp), tags=(tag,))

            self._row_count_lbl.config(
                text=f"Total records in file: {total:,}")

        except Exception as exc:
            self._row_count_lbl.config(text=f"Error loading data: {exc}")

    # -----------------------------------------------------------------------
    # Misc
    # -----------------------------------------------------------------------

    def _open_excel(self):
        path = Path(self.settings.get("excel_path", str(APP_DIR / "tnb_usage.xlsx")))
        if not path.exists():
            messagebox.showinfo("Not Found",
                                "Excel file not found. Run a sync first to create it.")
            return
        os.startfile(str(path))


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _extract_sdpudcid(cookie_str: str) -> str:
    """
    Try to pull sdpudcid from any cookie value that contains it.
    Tries multiple cookies (e3LoginReturn, dtSac*, etc.) and handles
    single and double URL-encoding.
    """
    import urllib.parse, re

    def _scan(text: str) -> str:
        """Return sdpudcid value if found anywhere in text."""
        # Direct match
        m = re.search(r'sdpudcid=([0-9]{10,})', text)
        if m:
            return m.group(1)
        return ""

    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, raw_val = part.partition("=")
        name = name.strip().lower()

        # Try the raw value first
        hit = _scan(raw_val)
        if hit:
            return hit

        # One level of URL-decoding
        try:
            decoded1 = urllib.parse.unquote(raw_val)
            hit = _scan(decoded1)
            if hit:
                return hit
            # Two levels (some cookies are double-encoded)
            decoded2 = urllib.parse.unquote(decoded1)
            hit = _scan(decoded2)
            if hit:
                return hit
        except Exception:
            pass

    return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = TNBApp()
    app.mainloop()
