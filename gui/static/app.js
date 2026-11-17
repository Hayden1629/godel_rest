// Gödel Relay control panel. Polls the loopback API and renders with DOM nodes
// (textContent only) — chat content is untrusted and never parsed as HTML.
"use strict";

const STATE_MS = 3000;
const MESSAGES_MS = 3000;
const LOG_MS = 4000;
const MESSAGE_LIMIT = 120;
const TOAST_MS = 6000;
const TICKER_RE = /\[([A-Za-z0-9.^=\/!-]+):(\d+)\]/g;

const ui = {
  channel: "",          // "" = all channels
  logName: "chat_poller",
  seenIds: new Set(),
  channels: new Map(),  // channel_id -> { title, count }
  firstLoad: true,
  lastStateAt: 0,
  services: {},
};

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

// ── api ───────────────────────────────────────────────────────────────────
async function api(path) {
  const res = await fetch(path, { cache: "no-store" });
  const body = await res.json();
  if (!body.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body.data;
}

async function runAction(action) {
  const res = await fetch("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Godel-GUI": "1" },
    body: JSON.stringify({ action }),
  });
  const body = await res.json();
  return { ok: body.ok, output: body.data?.output || "", error: body.error };
}

// ── formatting ────────────────────────────────────────────────────────────
function duration(secs) {
  const s = Math.abs(Math.round(secs));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${s}s`;
}

function ago(ms, nowMs) {
  if (!ms) return "—";
  return `${duration((nowMs - ms) / 1000)} ago`;
}

function clockTime(ms) {
  return new Date(ms).toLocaleTimeString([], { hour12: false });
}

function dateTime(ms) {
  return new Date(ms).toLocaleString([], {
    weekday: "short", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function setPill(node, text, cls) {
  node.textContent = text;
  node.className = "pill" + (cls ? " " + cls : "");
}

// ── toast ─────────────────────────────────────────────────────────────────
let toastTimer = null;
function toast(text, bad = false) {
  const node = $("toast");
  node.textContent = text;
  node.className = "toast" + (bad ? " bad" : "");
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, TOAST_MS);
}

// ── state cards ───────────────────────────────────────────────────────────
function renderToken(token, nowMs) {
  const bar = $("token-bar");
  if (!token.captured) {
    $("token-left").textContent = "—";
    $("token-exp").textContent = "No token yet — open app.godelterminal.com in Firefox";
    setPill($("token-pill"), "missing", "bad");
    bar.style.width = "0";
    return;
  }
  const left = token.seconds_left;
  const labels = { ok: ["valid", "ok"], warn: ["expiring", "warn"], expired: ["expired", "bad"] };
  const [label, cls] = labels[token.state] || ["?", ""];
  setPill($("token-pill"), label, cls);
  if (left == null) {
    $("token-left").textContent = "no expiry";
    $("token-exp").textContent = "";
    bar.style.width = "100%";
    return;
  }
  $("token-left").textContent = left > 0 ? duration(left) : `−${duration(left)}`;
  $("token-exp").textContent =
    (left > 0 ? "Expires " : "Expired ") + dateTime(token.exp * 1000) +
    (token.captured_at ? ` · captured ${ago(token.captured_at * 1000, nowMs)}` : "");
  const pct = Math.max(0, Math.min(100, (left / token.lifetime) * 100));
  bar.style.width = `${pct}%`;
  bar.className = "bar-fill" + (cls === "ok" ? "" : " " + cls);
}

function renderServices(svcs) {
  ui.services = svcs;
  for (const name of ["token_server", "chat_poller"]) {
    const s = svcs[name] || {};
    $(`dot-${name}`).className = "dot" + (s.running ? " on" : "");
    $(`pid-${name}`).textContent = s.running ? `pid ${s.pid}` : "stopped";
  }
  const relay = svcs.token_server?.running;
  const poller = svcs.chat_poller?.running;
  if (relay && poller) setPill($("watch-pill"), "watching", "ok");
  else if (relay || poller) setPill($("watch-pill"), "partial", "warn");
  else setPill($("watch-pill"), "stopped", "bad");
  $("btn-start").disabled = relay && poller;
  $("btn-stop").disabled = !relay && !poller;

  const bf = svcs.backfill?.running;
  setPill($("backfill-pill"), bf ? "running" : "idle", bf ? "ok" : "");
  $("btn-backfill").disabled = bf;
  $("btn-backfill-stop").disabled = !bf;
}

function renderArchive(db, alerts, nowMs) {
  $("db-total").textContent = db.total.toLocaleString();
  $("db-hour").textContent = db.last_hour.toLocaleString();
  $("db-newest").textContent = ago(db.newest_ms, nowMs);
  const node = $("alerts");
  node.textContent = alerts.scheduled ? "Scheduled" : "Not scheduled";
  node.className = alerts.scheduled ? "ok" : "warn";
  node.title = alerts.scheduled
    ? "server.token_watch runs from cron and pushes to ntfy"
    : "No cron entry for server.token_watch — you won't get expiry alerts";
}

async function refreshBackfillLine() {
  try {
    const { lines } = await api("/api/log?name=backfill");
    const last = lines.filter((l) => l.trim()).pop();
    $("backfill-last").textContent = last || "Fills the gap since the last run.";
    $("backfill-last").title = last || "";
  } catch (_) { /* card keeps its last text */ }
}

async function refreshState() {
  try {
    const s = await api("/api/state");
    renderToken(s.token, s.now_ms);
    renderServices(s.services);
    renderArchive(s.db, s.alerts, s.now_ms);
    ui.lastStateAt = Date.now();
    setPill($("live-pill"), "live", "ok");
    refreshBackfillLine();
  } catch (e) {
    setPill($("live-pill"), "offline", "bad");
    console.error("state refresh failed", e);
  }
}

// ── chat feed ─────────────────────────────────────────────────────────────
function contentNodes(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const m of text.matchAll(TICKER_RE)) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    frag.append(el("span", "ticker", m[1]));
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}

function messageRow(m, fresh) {
  const row = el("div", "msg" + (fresh ? " fresh" : ""));
  row.append(el("div", "msg-time", m.created_ms ? clockTime(m.created_ms) : "—"));
  const chan = el("div", "msg-chan");
  chan.append(el("span", "chan", `#${m.channel}`));
  row.append(chan);
  const body = el("div", "msg-body");
  if (m.parent_username) {
    const reply = el("span", "msg-reply",
      `↳ ${m.parent_username}: ${m.parent_content.replace(TICKER_RE, "$1")}`);
    body.append(reply);
  }
  body.append(el("span", "msg-user", m.username || "?"));
  const textNode = el("span", "msg-text");
  textNode.append(contentNodes(m.content));
  body.append(textNode);
  row.append(body);
  return row;
}

function noteChannels(messages) {
  if (ui.channel) return;  // only learn the chip set from the unfiltered feed
  for (const m of messages) {
    const c = ui.channels.get(m.channel_id) || { title: m.channel, count: 0 };
    ui.channels.set(m.channel_id, { title: m.channel, count: c.count + 1 });
  }
}

function renderChips() {
  const chips = $("chips");
  const make = (id, label) => {
    const b = el("button", "chip" + (ui.channel === id ? " active" : ""), label);
    b.addEventListener("click", () => {
      ui.channel = id;
      ui.firstLoad = true;  // don't flash the whole list on a filter switch
      renderChips();
      refreshMessages();
    });
    return b;
  };
  const sorted = [...ui.channels.entries()].sort((a, b) => b[1].count - a[1].count);
  chips.replaceChildren(make("", "all"), ...sorted.slice(0, 8).map(([id, c]) => make(id, `#${c.title}`)));
}

async function refreshMessages() {
  const qs = new URLSearchParams({ limit: String(MESSAGE_LIMIT) });
  if (ui.channel) qs.set("channel", ui.channel);
  let data;
  try {
    data = await api(`/api/messages?${qs}`);
  } catch (e) {
    console.error("messages refresh failed", e);
    return;
  }
  const feed = $("feed");
  const msgs = data.messages;
  if (!msgs.length) {
    feed.replaceChildren(el("div", "empty", "No messages yet."));
    return;
  }
  const rows = msgs.map((m) => {
    const fresh = !ui.firstLoad && !ui.seenIds.has(m.id);
    ui.seenIds.add(m.id);
    return messageRow(m, fresh);
  });
  const keepScroll = feed.scrollTop > 40;
  const prevHeight = feed.scrollHeight;
  const prevTop = feed.scrollTop;
  feed.replaceChildren(...rows);
  if (keepScroll) feed.scrollTop = prevTop + (feed.scrollHeight - prevHeight);
  ui.firstLoad = false;
  noteChannels(msgs);
  renderChips();
}

// ── logs ──────────────────────────────────────────────────────────────────
async function refreshLog() {
  const pre = $("log");
  try {
    const { lines } = await api(`/api/log?name=${encodeURIComponent(ui.logName)}`);
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
    pre.textContent = lines.length ? lines.join("\n") : "(no log yet)";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  } catch (e) {
    pre.textContent = `could not read log: ${e.message}`;
  }
}

function bindTabs() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      ui.logName = tab.dataset.log;
      const pre = $("log");
      pre.textContent = "";
      refreshLog().then(() => { pre.scrollTop = pre.scrollHeight; });
    });
  }
}

// ── actions ───────────────────────────────────────────────────────────────
function bindActions() {
  for (const btn of document.querySelectorAll("[data-action]")) {
    btn.addEventListener("click", async () => {
      const action = btn.dataset.action;
      btn.classList.add("busy");
      btn.disabled = true;
      try {
        const r = await runAction(action);
        if (r.ok) toast(r.output || `${action}: done`);
        else toast(`${r.error}${r.output ? "\n" + r.output : ""}`, true);
      } catch (e) {
        toast(`${action} failed: ${e.message}`, true);
      } finally {
        btn.classList.remove("busy");
        btn.disabled = false;
        await refreshState();
        refreshLog();
      }
    });
  }
}

// ── clock ─────────────────────────────────────────────────────────────────
function tick() {
  $("clock").textContent = clockTime(Date.now());
  if (ui.lastStateAt) {
    const secs = Math.round((Date.now() - ui.lastStateAt) / 1000);
    $("updated").textContent = `updated ${secs}s ago`;
  }
}

bindTabs();
bindActions();
refreshState();
refreshMessages();
refreshLog();
tick();
setInterval(refreshState, STATE_MS);
setInterval(refreshMessages, MESSAGES_MS);
setInterval(refreshLog, LOG_MS);
setInterval(tick, 1000);
