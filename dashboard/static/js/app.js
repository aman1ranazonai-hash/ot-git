/* ==========================================================================
   Kronos Trading Bot — dashboard frontend
   Auto-refresh: stats 5s · trades 10s · logs 2s · markets 30s
   PIN modal for sensitive actions; confirm dialogs; currency/percent formats
   ========================================================================== */

"use strict";

/* ----------------------------- Helpers --------------------------------- */
const $ = (id) => document.getElementById(id);

function fmtMoney(v) {
  const n = Number(v) || 0;
  const sign = n >= 0 ? "+" : "-";
  return `${sign}$${Math.abs(n) % 1 === 0 ? Math.abs(Math.round(n)) : Math.abs(n).toFixed(2)}`;
}

function fmtPct(v) {
  const n = Number(v) || 0;
  return `${n % 1 === 0 ? n : n.toFixed(1)}%`;
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function showMessage(el, text, ok) {
  el.textContent = text;
  el.className = "action-msg " + (ok ? "ok" : "err");
  setTimeout(() => { el.textContent = ""; }, 6000);
}

/* ------------------------- PIN modal (shared) --------------------------- */
const pinModal = {
  el: $("pin-modal"),
  input: $("pin-input"),
  submit: $("pin-submit"),
  error: $("pin-error"),
  desc: $("pin-desc"),
  title: $("pin-title"),
  onSubmit: null,

  open(desc, onSubmit) {
    this.desc.textContent = desc || "";
    this.input.value = "";
    this.submit.disabled = true;     // submit only enabled after PIN entered
    this.error.textContent = "";
    this.onSubmit = onSubmit;
    this.el.classList.remove("hidden");
    this.input.focus();
  },
  close() {
    this.el.classList.add("hidden");
    this.onSubmit = null;
  },
  pin() { return this.input.value.trim(); },
};

// Submit button only enabled once a PIN is entered (spec)
if (pinModal.input) {
  pinModal.input.addEventListener("input", () => {
    pinModal.submit.disabled = pinModal.pin().length === 0;
  });
  pinModal.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && pinModal.pin()) pinModal.submit.click();
  });
  pinModal.submit.addEventListener("click", async () => {
    if (pinModal.onSubmit) await pinModal.onSubmit(pinModal.pin());
  });
  $("pin-cancel").addEventListener("click", () => pinModal.close());
  pinModal.el.addEventListener("click", (e) => {
    if (e.target === pinModal.el) pinModal.close();
  });
}

/* ------------------------- API communication ---------------------------- */
async function apiGet(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function apiPost(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, data };
}

/* --------------------------- Command dispatch --------------------------- */
const isDashboard = !!$("stat-market");

function runCommand(command, extra = {}, needsPin = false, pinDesc = "") {
  const send = async (pin = null) => {
    const { ok, data } = await apiPost("/api/command", { command, pin, ...extra });
    const msgEl = $("control-msg");
    if (ok && data.success) {
      showMessage(msgEl, `✅ ${data.message || "Action complete!"}`, true);
      if (pinModal.el && !pinModal.el.classList.contains("hidden")) pinModal.close();
      refreshStatus();
    } else if (data.locked) {
      if (msgEl) showMessage(msgEl, data.message, false);
      pinModal.error.textContent = data.message;
    } else if (data.message && data.message.includes("Wrong PIN")) {
      pinModal.error.textContent = data.message;   // keep modal open, show attempts left
    } else {
      if (msgEl) showMessage(msgEl, data.message || "⚠️ Error processing command", false);
      if (!pinModal.el.classList.contains("hidden")) pinModal.close();
    }
  };

  if (needsPin) {
    pinModal.open(pinDesc, async (pin) => {
      pinModal.error.textContent = "";
      await send(pin);
    });
  } else {
    send();
  }
}

/* --------------------------- Refresh: status ---------------------------- */
async function refreshStatus() {
  try {
    const s = await apiGet("/api/status");
    if (!isDashboard) return;
    const pos = s.pnl >= 0;

    setText("stat-market", s.market || "Closed");
    setText("stat-market-window", s.market_window || "Outside market hours");
    setText("stat-signal", s.signal);
    const sigKey = (s.signal || "hold").toLowerCase().replace(/_/g, "");
    $("stat-signal").className = "stat-value sig-" + sigKey;
    setText("stat-confidence", fmtPct(s.confidence));
    if (s.threshold != null) {
      setText("stat-threshold", `trade if > ${fmtPct(s.threshold)}`);
    }
    setText("stat-pnl", s.pnl_formatted);
    $("stat-pnl").className = "stat-value " + (pos ? "pos" : "neg");
    setText("stat-trades", s.trades_today);
    setText("stat-wl", `${s.wins ?? 0}W / ${s.losses ?? 0}L`);
    setText("stat-winrate", fmtPct(s.win_rate));
    setText("stat-next-switch", s.next_switch || "—");
    setText("stat-countdown", s.next_switch_countdown ? `in ${s.next_switch_countdown}` : "");

    // navbar
    const badge = $("mode-badge");
    badge.textContent = s.mode;
    badge.className = "badge " + (s.mode === "REAL" ? "badge-real" : "badge-demo");
    const navPnl = $("nav-pnl");
    navPnl.textContent = s.pnl_formatted;
    navPnl.className = "nav-pnl " + (pos ? "pos" : "neg");
    const dbFlag = s.db_connected ? "· DB ✅" : "· DB ⚠️ (fallback)";
    setText("last-update", `Updated ${s.updated_at} ${dbFlag}`);

    // reflect state in control buttons
    // START stays clickable while the bot is paused after a loss — that click
    // is the manual approval that resumes trading.
    $("btn-start").disabled = !!s.running && !s.force_stop && !s.paused;
    $("btn-stop").disabled = !s.running && !s.force_stop ? false : !!s.force_stop && !s.running;

    // pause banner (PAUSED_AFTER_LOSS / PAUSED_MANUAL)
    const banner = $("pause-banner");
    if (banner) {
      if (s.paused) {
        banner.classList.remove("hidden");
        setText("pause-banner-status", s.bot_status || "PAUSED");
        setText("pause-banner-reason", s.last_loss_reason || "Awaiting manual approval.");
      } else {
        banner.classList.add("hidden");
      }
    }
  } catch (e) {
    // Spec: on DB failure show 'Loading data...' and keep retrying (5s interval)
    setText("stat-pnl", "Loading data...");
    setText("stat-market", "Loading data...");
    setText("last-update", `Retrying every 5s (${e.message})`);
  }
}

/* --------------------------- Refresh: trades ---------------------------- */
async function refreshTrades() {
  if (!isDashboard) return;
  try {
    const trades = await apiGet("/api/trades");
    const body = $("trades-body");
    if (!Array.isArray(trades) || trades.length === 0) {
      body.innerHTML = '<tr><td colspan="8" class="empty-row">No trades recorded yet</td></tr>';
      return;
    }
    body.innerHTML = trades.map((t) => {
      const pnl = t.pnl === null || t.pnl === undefined ? "—" : fmtMoney(t.pnl);
      const pnlCls = t.pnl > 0 ? "pnl-pos" : t.pnl < 0 ? "pnl-neg" : "";
      const sig = (t.signal || "?").toUpperCase();
      const sigCls = (sig === "BUY" || sig === "UP") ? "buy"
        : (sig === "SELL" || sig === "DOWN") ? "sell" : "";
      // DB confidence is 0-1 (0.78 = 78%); log fallback may already be null
      const conf = t.confidence != null
        ? (t.confidence <= 1 ? fmtPct(t.confidence * 100) : fmtPct(t.confidence))
        : "—";
      const status = (t.status || "closed");
      return `<tr>
        <td>${t.timestamp ?? "—"}</td>
        <td>${t.symbol ?? "—"}</td>
        <td>${sigCls ? `<span class="sig-badge ${sigCls}">${sig}</span>` : sig}</td>
        <td>${conf}</td>
        <td>${t.entry_price != null ? t.entry_price : "—"}</td>
        <td>${t.exit_price != null ? t.exit_price : "—"}</td>
        <td class="${pnlCls}">${pnl}</td>
        <td class="${/open/i.test(status) ? "status-open" : "status-closed"}">${status}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    const body = $("trades-body");
    if (body) body.innerHTML = '<tr><td colspan="8" class="empty-row">Loading data...</td></tr>';
  }
}

/* ---------------------------- Refresh: logs ----------------------------- */
function classifyLogEntry(entry) {
  // entry: {timestamp, event, message} from the DB (or a raw line fallback)
  const text = typeof entry === "string"
    ? entry
    : `${entry.event || ""} ${entry.message || ""}`;
  const event = (typeof entry === "object" && entry.event) || "";
  const l = text.toLowerCase();
  if (event === "ERROR" || l.includes("error") || l.includes("failed") ||
      /-\$[0-9]/.test(text)) return "log-err";
  if (event === "PIN_WRONG" || l.includes("warning") || l.includes("warn")) return "log-warn";
  if (event === "TRADE" && l.includes("closed")) return /\+\$/.test(text) ? "log-ok" : "log-err";
  if (event === "TRADE" || event === "PREDICTION" || l.includes("buy") ||
      l.includes("started") || l.includes("reached") || /\+\$[0-9]/.test(text)) return "log-ok";
  if (event === "MARKET_SWITCH" || event === "STOP" || event === "FORCESTOP" ||
      l.includes("switch") || l.includes("window")) return "log-info";
  return "log-dim";
}

function formatLogEntry(entry) {
  if (typeof entry === "string") return escapeHtml(entry);
  const ts = entry.timestamp ? String(entry.timestamp) : "—";
  return escapeHtml(`[${ts}] [${entry.event || "INFO"}] ${entry.message || ""}`);
}

async function refreshLogs() {
  if (!isDashboard) return;
  try {
    const entries = await apiGet("/api/logs");
    const box = $("log-box");
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    box.innerHTML = (Array.isArray(entries) ? entries : [])
      .map((entry) => `<div class="log-line ${classifyLogEntry(entry)}">${formatLogEntry(entry)}</div>`)
      .join("");
    if (nearBottom) box.scrollTop = box.scrollHeight;   // auto-scroll to bottom
  } catch (e) {
    const box = $("log-box");
    if (box) box.innerHTML = '<div class="log-line log-warn">Loading data... (retrying every 2s)</div>';
  }
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* --------------------------- Refresh: markets --------------------------- */
async function refreshMarkets() {
  if (!isDashboard) return;
  try {
    const data = await apiGet("/api/markets");
    const list = $("market-list");
    list.innerHTML = (data.markets || []).map((m) => `
      <div class="market-item ${m.active ? "active" : m.is_next ? "next" : ""}">
        <div>
          <div class="market-name">${m.symbol} <span class="market-symbol">· ${m.name}</span></div>
          <div class="market-times">${m.start} – ${m.end} IST</div>
        </div>
        <div style="text-align:right">
          ${m.active
            ? `<div class="market-status live">● LIVE</div><div class="market-countdown">${m.countdown ?? ""}</div>`
            : m.is_next ? '<div class="market-status next-up">NEXT</div>' : ""}
        </div>
      </div>`).join("");
    setText("market-countdown", data.countdown || "—");
  } catch (e) { /* keep last list */ }
}

/* ------------------------------- Wiring --------------------------------- */
function initDashboard() {
  // Simple actions: no PIN
  $("btn-start").addEventListener("click", () =>
    confirmDialog("Resume trading?", () => runCommand("start")));
  $("btn-stop").addEventListener("click", () =>
    confirmDialog("Stop the bot? No new trades will be placed.", () => runCommand("stop")));

  // PIN-protected actions
  $("btn-demo").addEventListener("click", () =>
    runCommand("demo", {}, true, "Switch to DEMO mode"));
  $("btn-real").addEventListener("click", () =>
    confirmDialog("Switch to REAL mode? Real money is at risk!", () =>
      runCommand("real", {}, true, "Switch to REAL mode")));
  $("btn-forcestop").addEventListener("click", () =>
    confirmDialog("EMERGENCY STOP all trading now?", () =>
      runCommand("forcestop", {}, true, "force-stop the bot")));

  $("btn-set-token").addEventListener("click", () => {
    const token = $("token-input").value.trim();
    if (!token) return showMessage($("control-msg"), "Enter a token first", false);
    pinModal.open("Update Olymp token", async (pin) => {
      const { ok, data } = await apiPost("/api/command", { command: "set_token", token, pin });
      if (ok && data.success) {
        $("token-input").value = "";
        pinModal.close();
        showMessage($("control-msg"), "✅ Token updated!", true);
      } else if (data.message && data.message.includes("Wrong PIN")) {
        pinModal.error.textContent = data.message;
      } else {
        pinModal.close();
        showMessage($("control-msg"), data.message || "⚠️ Error", false);
      }
    });
  });

  $("btn-set-target").addEventListener("click", () => {
    const amount = parseFloat($("target-amount").value);
    if (!amount || amount <= 0) return showMessage($("control-msg"), "Enter a valid target amount", false);
    pinModal.open(`Set daily target to $${amount}`, async (pin) => {
      const { ok, data } = await apiPost("/api/command",
        { command: "set_target", amount, enabled: $("target-toggle").checked, pin });
      if (ok && data.success) {
        pinModal.close();
        showMessage($("control-msg"), "✅ Target enabled! Will stop at " + fmtMoney(amount), true);
      } else if (data.message && data.message.includes("Wrong PIN")) {
        pinModal.error.textContent = data.message;
      } else {
        pinModal.close();
        showMessage($("control-msg"), data.message || "⚠️ Error", false);
      }
    });
  });

  $("target-toggle").addEventListener("change", () => {
    setText("target-state", $("target-toggle").checked ? "ON" : "OFF");
  });

  // initial load + intervals (spec: stats 5s, trades 10s, logs 2s)
  refreshStatus(); refreshTrades(); refreshLogs(); refreshMarkets();
  setInterval(refreshStatus, 5000);
  setInterval(refreshTrades, 10000);
  setInterval(refreshLogs, 2000);
  setInterval(refreshMarkets, 30000);
}

/* ------------------------- Settings page logic --------------------------- */
function initSettings() {
  const editableList = $("editable-list");
  const saveBtn = $("save-settings");
  const msgEl = $("settings-msg");
  let current = {};

  async function load() {
    try {
      const data = await apiGet("/api/settings");
      current = Object.fromEntries((data.editable || []).map((s) => [s.key, s.value]));

      $("readonly-body").innerHTML = (data.readonly || []).map((s) => `
        <tr><td>${s.label}</td><td><code>${s.key}</code></td>
        <td>${escapeHtml(s.value || "—")}</td></tr>`).join("");

      editableList.innerHTML = (data.editable || []).map((s) => `
        <div class="edit-row">
          <label for="set-${s.key}">${s.label} <code>${s.key}</code></label>
          <input id="set-${s.key}" type="${s.type === 'number' ? 'number' : 'text'}"
                 step="any" value="${escapeHtml(s.value ?? "")}">
        </div>`).join("");
      saveBtn.disabled = false;
    } catch (e) {
      msgEl.textContent = "Failed to load settings: " + e.message;
      msgEl.className = "action-msg err";
    }
  }

  saveBtn.addEventListener("click", () => {
    const updates = {};
    for (const key of Object.keys(current)) {
      const el = $(`set-${key}`);
      if (el && el.value !== current[key]) updates[key] = el.value;
    }
    if (Object.keys(updates).length === 0) {
      return showMessage(msgEl, "No changes to save", false);
    }
    pinModal.open("save settings changes", async (pin) => {
      const { ok, data } = await apiPost("/api/settings", { pin, updates });
      if (ok && data.success) {
        pinModal.close();
        showMessage(msgEl, data.message, true);
        load();
      } else if (data.message && data.message.includes("Wrong PIN")) {
        pinModal.error.textContent = data.message;
      } else {
        pinModal.close();
        showMessage(msgEl, data.message || "⚠️ Error saving", false);
      }
    });
  });

  load();
}

/* ---------------------------- Confirm dialog ---------------------------- */
function confirmDialog(message, onYes) {
  // Minimal native confirm; keeps sensitive actions deliberate
  if (window.confirm(message)) onYes();
}

/* ------------------------------ Bootstrap ------------------------------- */
document.addEventListener("DOMContentLoaded", () => {
  if ($("stat-market")) initDashboard();          // dashboard page
  else if ($("editable-list")) initSettings();    // settings page
});
