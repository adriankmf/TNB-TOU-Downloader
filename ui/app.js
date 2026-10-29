/* TNB Smart Sync front end. Talks to app.py via window.pywebview.api.* (each returns a Promise). */
"use strict";

const $ = (id) => document.getElementById(id);
const COLORS = { peak: "#f2726f", off: "#4aa8ff", accent: "#22c3a6", grid: "rgba(139,152,168,.12)", text: "#8b98a8" };
const charts = {};
let state = null;
let range = "30";

/* ── Events pushed from Python ─────────────────────────────────────── */
window.tnb = {
  on(kind, payload) {
    if (kind === "log") addLog(payload);
    if (kind === "progress") setProgress(payload);
  },
};

function addLog({ time, text }) {
  for (const list of [$("log"), $("loginLog")]) {
    const li = document.createElement("li");
    const t = document.createElement("time");
    t.textContent = time;
    li.append(t, document.createTextNode(text));
    list.append(li);
    list.scrollTop = list.scrollHeight;
  }
  $("logLast").textContent = text;
}

function setProgress(value) {
  const bar = $("progress");
  if (value === null) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  bar.classList.toggle("indeterminate", value < 0);
  $("progressBar").style.width = value < 0 ? "" : `${Math.round(value * 100)}%`;
}

function toast(text, kind = "") {
  const el = $("toast");
  el.textContent = text;
  el.className = `toast ${kind}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add("hidden"), kind === "err" ? 9000 : 5000);
}

async function call(name, ...args) {
  try {
    return await window.pywebview.api[name](...args);
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

function setBusy(busy) {
  for (const id of ["syncBtn", "refreshAccountsBtn", "fillGapsBtn", "loginBtn", "accountSelect"]) $(id).disabled = busy;
  setProgress(busy ? -1 : null);
}

/* ── Views ─────────────────────────────────────────────────────────── */
function render(newState) {
  state = newState;
  const signedIn = state.signed_in && state.accounts.length > 0;
  $("loginView").classList.toggle("hidden", signedIn);
  $("mainView").classList.toggle("hidden", !signedIn);
  if (!signedIn) {
    $("email").value = $("email").value || state.email || "";
    ($("email").value ? $("password") : $("email")).focus();
    return;
  }
  const select = $("accountSelect");
  select.replaceChildren(...state.accounts.map((a) => {
    const opt = document.createElement("option");
    opt.value = a.ca_no;
    opt.textContent = `${a.name} — ${a.ca_no}`;
    return opt;
  }));
  select.value = state.selected_account;
  $("signedInAs").textContent = state.email;
}

async function loadDashboard() {
  if (!state || !state.selected_account) return;
  const data = await call("usage", state.selected_account, range);
  if (!data.ok) { toast(data.error, "err"); return; }
  const empty = data.empty;
  $("emptyState").classList.toggle("hidden", !empty);
  $("dashboard").classList.toggle("hidden", empty);
  const gaps = data.gaps || [];
  $("gapBadge").classList.toggle("hidden", !gaps.length);
  $("fillGapsBtn").classList.toggle("hidden", !gaps.length);
  $("gapBadge").textContent = `${gaps.length} incomplete day${gaps.length === 1 ? "" : "s"}`;
  $("gapBadge").title = gaps.slice(0, 40).join(", ") + (gaps.length > 40 ? " …" : "");
  if (empty) { $("statusLatest").textContent = "No data saved yet"; return; }

  const k = data.kpi;
  $("statusLatest").textContent = `Data up to ${k.latest}  ·  ${k.rows.toLocaleString()} readings since ${k.first.slice(0, 10)}`;
  $("kTotal").textContent = `${fmt(k.total_kwh)} kWh`;
  $("kDays").textContent = `${k.days} day${k.days === 1 ? "" : "s"}`;
  $("kAvg").textContent = `${fmt(k.avg_daily_kwh, 1)} kWh`;
  $("kMax").textContent = k.max_day ? `Highest: ${fmt(k.max_day_kwh, 1)} kWh on ${k.max_day}` : "";
  $("kPeak").textContent = `${k.peak_pct}%`;
  $("kPeakKwh").textContent = `${fmt(k.peak_kwh)} kWh in peak hours`;
  $("kOff").textContent = `${fmt(k.offpeak_kwh)} kWh`;
  $("kEst").textContent = k.estimated_pct > 0
    ? `${k.estimated_pct}% of readings pre-TOU: split estimated by the TOU rule`
    : "Split from TNB's TOU categories";
  drawCharts(data);
}

const fmt = (n, digits = 0) => Number(n).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });

/* ── Charts ────────────────────────────────────────────────────────── */
function baseOptions(stacked, unit = "kWh") {
  return {
    responsive: true, maintainAspectRatio: false, animation: { duration: 350 },
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { labels: { color: COLORS.text, boxWidth: 12, usePointStyle: true } },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y == null ? "–" : c.parsed.y.toFixed(2)} ${unit}` } },
    },
    scales: {
      x: { stacked, ticks: { color: COLORS.text, maxRotation: 0, autoSkipPadding: 14 }, grid: { display: false } },
      y: { stacked, ticks: { color: COLORS.text }, grid: { color: COLORS.grid }, beginAtZero: true },
    },
  };
}

function upsert(id, config) {
  if (typeof Chart === "undefined") return;
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id), config);
}

function drawCharts(d) {
  if (typeof Chart === "undefined") { toast("Charts need internet (Chart.js could not load).", "err"); return; }
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  const bar = (label, data, color) => ({ label, data, backgroundColor: color, borderRadius: 3, maxBarThickness: 28 });

  upsert("dailyChart", { type: "bar", data: { labels: d.daily.labels.map((s) => s.slice(5)),
    datasets: [bar("Peak", d.daily.peak, COLORS.peak), bar("Off-peak", d.daily.offpeak, COLORS.off)] },
    options: baseOptions(true) });

  upsert("monthlyChart", { type: "bar", data: { labels: d.monthly.labels,
    datasets: [bar("Peak", d.monthly.peak, COLORS.peak), bar("Off-peak", d.monthly.offpeak, COLORS.off)] },
    options: baseOptions(true) });

  upsert("splitChart", { type: "doughnut", data: { labels: ["Peak", "Off-peak"],
    datasets: [{ data: [d.kpi.peak_kwh, d.kpi.offpeak_kwh], backgroundColor: [COLORS.peak, COLORS.off], borderWidth: 0 }] },
    options: { responsive: true, maintainAspectRatio: false, cutout: "68%",
      plugins: { legend: { position: "bottom", labels: { color: COLORS.text, usePointStyle: true } } } } });

  const line = (label, data, color) => ({ label, data, borderColor: color, backgroundColor: color + "22", fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2 });
  upsert("profileChart", { type: "line", data: { labels: d.profile.labels,
    datasets: [line("Weekday", d.profile.weekday, COLORS.accent), line("Weekend", d.profile.weekend, "#b48cff")] },
    options: baseOptions(false) });

  $("latestTitle").textContent = `Latest full day · ${d.latest_day.date}`;
  upsert("latestChart", { type: "bar", data: { labels: d.latest_day.labels,
    datasets: [{ label: "kWh", data: d.latest_day.kwh, borderRadius: 2,
      backgroundColor: d.latest_day.peak.map((p) => (p ? COLORS.peak : COLORS.off)) }] },
    options: { ...baseOptions(false), plugins: { legend: { display: false } } } });
}

/* ── Actions ───────────────────────────────────────────────────────── */
async function doLogin(ev) {
  ev.preventDefault();
  $("loginError").classList.add("hidden");
  $("loginLog").replaceChildren();
  setBusy(true);
  $("loginBtn").textContent = "Signing in…";
  const res = await call("login", $("email").value, $("password").value);
  setBusy(false);
  $("loginBtn").textContent = "Sign in";
  if (!res.ok) {
    $("loginError").textContent = res.error;
    $("loginError").classList.remove("hidden");
    return;
  }
  $("password").value = "";
  render(res);
  loadDashboard();
}

async function doSync(fillGaps = false) {
  setBusy(true);
  $("logPanel").classList.remove("collapsed");
  const res = await call("sync", state.selected_account, fillGaps);
  setBusy(false);
  if (!res.ok) {
    toast(res.error, "err");
    if (/sign in again|enter your email/i.test(res.error)) render({ ...state, signed_in: false });
    return;
  }
  const r = res.result;
  render(res.state);
  let msg = `${r.rows_added.toLocaleString()} new reading(s). Data up to ${r.latest || "–"}.`;
  if (r.unavailable.length) msg += ` TNB had incomplete data for ${r.unavailable.length} day(s).`;
  if (r.stopped) msg += ` Stopped early: ${r.stopped} Try again later.`;
  toast(msg, r.stopped ? "err" : "good");
  loadDashboard();
}

async function doRefreshAccounts() {
  setBusy(true);
  const res = await call("refresh_accounts");
  setBusy(false);
  if (!res.ok) { toast(res.error, "err"); return; }
  render(res);
  toast(`${res.accounts.length} account(s) loaded.`, "good");
  loadDashboard();
}

function openSettings() {
  $("folderInput").value = state.excel_folder;
  $("browserInput").value = state.browser;
  $("startInput").value = state.bootstrap_start;
  $("settingsError").classList.add("hidden");
  $("settingsDlg").showModal();
}

async function saveSettings() {
  const res = await call("save_prefs", { browser: $("browserInput").value, bootstrap_start: $("startInput").value });
  if (!res.ok) {
    $("settingsError").textContent = res.error;
    $("settingsError").classList.remove("hidden");
    return;
  }
  render(res);
  $("settingsDlg").close();
  toast("Settings saved.", "good");
}

async function openPath(which) {
  const res = await call("open_path", which, state.selected_account);
  if (!res.ok) toast(res.error, "err");
}

function wire() {
  $("loginForm").addEventListener("submit", doLogin);
  $("syncBtn").addEventListener("click", () => doSync(false));
  $("fillGapsBtn").addEventListener("click", () => doSync(true));
  $("refreshAccountsBtn").addEventListener("click", doRefreshAccounts);
  $("accountSelect").addEventListener("change", async (e) => {
    const res = await call("select_account", e.target.value);
    if (res.ok) { render(res); loadDashboard(); }
  });
  $("rangeSeg").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    range = btn.dataset.range;
    for (const b of $("rangeSeg").children) b.classList.toggle("active", b === btn);
    loadDashboard();
  });
  $("settingsBtn").addEventListener("click", openSettings);
  $("saveSettingsBtn").addEventListener("click", saveSettings);
  $("pickFolderBtn").addEventListener("click", async () => {
    const res = await call("choose_folder");
    if (res.ok) { render(res); $("folderInput").value = res.excel_folder; loadDashboard(); }
  });
  $("signOutBtn").addEventListener("click", async () => {
    const res = await call("sign_out");
    $("settingsDlg").close();
    render(res);
  });
  $("logBtn").addEventListener("click", () => openPath("log"));
  $("openFile").addEventListener("click", (e) => { e.preventDefault(); openPath("file"); });
  $("openFolder").addEventListener("click", (e) => { e.preventDefault(); openPath("folder"); });
  $("logToggle").addEventListener("click", () => $("logPanel").classList.toggle("collapsed"));
}

window.addEventListener("pywebviewready", async () => {
  wire();
  $("logPanel").classList.add("collapsed");
  render(await call("get_state"));
  setBusy(true);
  const res = await call("startup");  // one-off move of the old Excel file; can take a while on Drive
  setBusy(false);
  if (!res.ok) toast(res.error, "err");
  else render(res);
  loadDashboard();
});
