// Stats view: tiles, daily volume, hour-of-day, and ranked lists. Charts are
// hand-built SVG (no library, works offline). Every series here is single, so
// one accent color and no legend; values reach the reader through the axis,
// hover tooltips, and the daily chart's table view. Text via textContent only.
"use strict";

const GodelStats = (() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const STATS_REFRESH_MS = 60000;
  const BAR_MAX_W = 24;
  const BAR_GAP = 2;
  const BAR_RADIUS = 4;
  const CHART_H = 220;
  const PAD = { top: 12, right: 8, bottom: 26, left: 44 };
  const LIST_ROWS = 15;

  const st = {
    days: 90,
    channel: "",          // "" = all channels
    channelTitle: "",
    overview: null,
    daily: [],
    timer: null,
    active: false,
  };

  const $ = (id) => document.getElementById(id);
  const fmtInt = (n) => n.toLocaleString();
  const fmtCompact = (n) =>
    n >= 10000 ? `${+(n / 1000).toFixed(n >= 100000 ? 0 : 1)}K` : fmtInt(n);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function svg(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    return node;
  }

  async function api(path) {
    const res = await fetch(path, { cache: "no-store" });
    const body = await res.json();
    if (!body.ok) throw new Error(body.error || `HTTP ${res.status}`);
    return body.data;
  }

  // ── tooltip ──────────────────────────────────────────────────────────────
  function showTip(evt, value, label) {
    const tip = $("tooltip");
    tip.replaceChildren(el("div", "tip-value mono", value), el("div", "tip-label", label));
    tip.hidden = false;
    const pad = 14;
    const { innerWidth: w } = window;
    const r = tip.getBoundingClientRect();
    let x = evt.clientX + pad;
    if (x + r.width > w - 8) x = evt.clientX - r.width - pad;
    tip.style.left = `${x}px`;
    tip.style.top = `${Math.max(8, evt.clientY - r.height - pad)}px`;
  }

  function hideTip() {
    $("tooltip").hidden = true;
  }

  // ── column chart ─────────────────────────────────────────────────────────
  function niceStep(max, ticks) {
    const raw = Math.max(1, max) / ticks;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const norm = raw / mag;
    const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
    return Math.max(1, nice * mag);
  }

  // rounded data-end (top), square at the baseline
  function columnPath(x, y, w, h) {
    const r = Math.min(BAR_RADIUS, w / 2, h);
    const b = y + h;
    return `M${x},${b}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}` +
           `Q${x + w},${y} ${x + w},${y + r}V${b}Z`;
  }

  // rows: [{label, value, tipLabel}]; labelEvery: show every Nth x label
  function columnChart(container, rows, labelEvery) {
    const width = Math.max(240, container.clientWidth);
    const plotW = width - PAD.left - PAD.right;
    const plotH = CHART_H - PAD.top - PAD.bottom;
    const max = Math.max(0, ...rows.map((r) => r.value));
    const step = niceStep(max, 4);
    const top = Math.max(step, Math.ceil(max / step) * step);
    const y = (v) => PAD.top + plotH - (v / top) * plotH;
    const slot = plotW / Math.max(1, rows.length);
    const barW = Math.max(1, Math.min(BAR_MAX_W, slot - BAR_GAP));

    const root = svg("svg", { width, height: CHART_H, class: "chart-svg", role: "img" });
    for (let v = 0; v <= top; v += step) {
      root.append(svg("line", { x1: PAD.left, x2: width - PAD.right, y1: y(v), y2: y(v),
                                class: v === 0 ? "axis" : "grid" }));
      const t = svg("text", { x: PAD.left - 8, y: y(v) + 4, class: "tick", "text-anchor": "end" });
      t.textContent = fmtCompact(v);
      root.append(t);
    }
    rows.forEach((r, i) => {
      const x = PAD.left + i * slot + (slot - barW) / 2;
      const h = (r.value / top) * plotH;
      const g = svg("g", { class: "col" });
      if (h > 0) g.append(svg("path", { d: columnPath(x, y(r.value), barW, h), class: "bar" }));
      // hit target: the whole slot, full height — bigger than the mark
      const hit = svg("rect", { x: PAD.left + i * slot, y: PAD.top, width: slot,
                                height: plotH, class: "hit" });
      hit.addEventListener("pointermove", (e) => showTip(e, fmtInt(r.value), r.tipLabel));
      hit.addEventListener("pointerleave", hideTip);
      g.append(hit);
      root.append(g);
      if (i % labelEvery === 0) {
        const t = svg("text", { x: x + barW / 2, y: CHART_H - 8, class: "tick",
                                "text-anchor": "middle" });
        t.textContent = r.label;
        root.append(t);
      }
    });
    container.replaceChildren(root);
  }

  // ── daily volume ─────────────────────────────────────────────────────────
  function dayLabel(iso, withWeekday) {
    const d = new Date(`${iso}T00:00:00Z`);
    const opts = { month: "short", day: "numeric", timeZone: "UTC" };
    if (withWeekday) opts.weekday = "short";
    return d.toLocaleDateString([], opts);
  }

  function renderDaily() {
    const rows = st.daily.map((r) => ({
      label: dayLabel(r.day, false),
      value: r.count,
      tipLabel: `${dayLabel(r.day, true)} · messages`,
    }));
    const every = st.days <= 30 ? 5 : st.days <= 90 ? 14 : 30;
    columnChart($("daily-chart"), rows, every);
    $("daily-title").textContent = st.channel
      ? `Messages per day · #${st.channelTitle}` : "Messages per day";
    const total = st.daily.reduce((a, r) => a + r.count, 0);
    $("daily-sub").textContent =
      `${fmtInt(total)} messages over ${st.days} days · UTC days`;
    renderDailyTable();
  }

  function renderDailyTable() {
    const table = el("table", "data-table");
    const head = el("tr");
    head.append(el("th", null, "Day (UTC)"), el("th", "num", "Messages"));
    table.append(head);
    for (const r of [...st.daily].reverse()) {
      const tr = el("tr");
      tr.append(el("td", "mono", r.day), el("td", "num mono", fmtInt(r.count)));
      table.append(tr);
    }
    $("daily-table").replaceChildren(table);
  }

  async function loadDaily() {
    const qs = new URLSearchParams({ days: String(st.days) });
    if (st.channel) qs.set("channel", st.channel);
    try {
      const data = await api(`/api/daily?${qs}`);
      st.daily = data.rows;
      renderDaily();
    } catch (e) {
      $("daily-chart").replaceChildren(el("div", "empty", `Could not load: ${e.message}`));
    }
  }

  // ── hour of day (UTC buckets shifted to local) ───────────────────────────
  function renderHourly(hourlyUtc) {
    const offsetH = -new Date().getTimezoneOffset() / 60;
    const local = Array(24).fill(0);
    hourlyUtc.forEach((n, h) => {
      local[(((h + Math.round(offsetH)) % 24) + 24) % 24] += n;
    });
    const hourName = (h) => `${String(h).padStart(2, "0")}:00`;
    const rows = local.map((n, h) => ({
      label: hourName(h),
      value: n,
      tipLabel: `${hourName(h)}–${hourName((h + 1) % 24)} · messages, last 30 days`,
    }));
    columnChart($("hourly-chart"), rows, 6);
    const tz = new Intl.DateTimeFormat([], { timeZoneName: "short" })
      .formatToParts(new Date()).find((p) => p.type === "timeZoneName")?.value || "local";
    $("hourly-sub").textContent = `Last 30 days, ${tz}`;
  }

  // ── ranked lists ─────────────────────────────────────────────────────────
  function rankRow(name, value, max, opts = {}) {
    const row = el(opts.onClick ? "button" : "div", "rank-row" + (opts.active ? " active" : ""));
    const label = el("div", "rank-name");
    label.append(opts.nameNode || el("span", null, name));
    if (opts.sub) label.append(el("span", "rank-sub", opts.sub));
    const track = el("div", "rank-track");
    const fill = el("div", "rank-fill");
    fill.style.width = `${max ? Math.max(1, (value / max) * 100) : 0}%`;
    track.append(fill);
    row.append(label, track, el("div", "rank-val mono", fmtInt(value)));
    if (opts.extra) row.append(el("div", "rank-extra mono", opts.extra));
    if (opts.onClick) row.addEventListener("click", opts.onClick);
    return row;
  }

  function ago(ms) {
    if (!ms) return "—";
    const s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  }

  function renderChannels(chans) {
    const max = Math.max(0, ...chans.map((c) => c.total));
    const head = el("div", "rank-head");
    head.append(el("span", null, "Channel"), el("span"), el("span", "num", "All time"),
                el("span", "num", "7 days"));
    const rows = chans.map((c) => rankRow(`#${c.title}`, c.total, max, {
      sub: `last ${ago(c.newest_ms)}`,
      extra: fmtInt(c.last_7d),
      active: st.channel === c.id,
      onClick: () => setChannel(st.channel === c.id ? "" : c.id, c.title),
    }));
    $("channels-list").replaceChildren(head, ...rows);
  }

  function renderTickers(tickers) {
    const max = Math.max(0, ...tickers.map((t) => t.count));
    const rows = tickers.slice(0, LIST_ROWS).map((t) => {
      const name = el("span", "ticker", t.ticker);
      return rankRow(t.ticker, t.count, max, { nameNode: name, sub: t.name || "" });
    });
    $("tickers-list").replaceChildren(...(rows.length ? rows : [el("div", "empty", "No mentions.")]));
  }

  function renderUsers(users) {
    const max = Math.max(0, ...users.map((u) => u.count));
    const rows = users.slice(0, LIST_ROWS).map((u) => rankRow(u.username, u.count, max));
    $("users-list").replaceChildren(...(rows.length ? rows : [el("div", "empty", "No messages.")]));
  }

  // ── tiles ────────────────────────────────────────────────────────────────
  function renderTiles(t, nChannels) {
    $("t-24h").textContent = fmtInt(t.last_24h);
    const delta = $("t-24h-delta");
    if (t.week_ago_24h > 0) {
      const pct = Math.round(((t.last_24h - t.week_ago_24h) / t.week_ago_24h) * 100);
      delta.textContent = `${pct >= 0 ? "▲" : "▼"} ${Math.abs(pct)}% vs same time last week`;
    } else {
      delta.textContent = "no messages this time last week";
    }
    $("t-avg").textContent = fmtInt(t.avg_per_day_7d);
    $("t-users").textContent = fmtInt(t.active_users_7d);
    $("t-total").textContent = fmtInt(t.total);
    $("t-channels").textContent = `across ${nChannels} channels`;
  }

  // ── loading ──────────────────────────────────────────────────────────────
  async function loadOverview() {
    try {
      const o = await api("/api/stats");
      st.overview = o;
      renderTiles(o.tiles, o.channels.length);
      renderHourly(o.hourly_utc);
      renderChannels(o.channels);
      renderTickers(o.tickers_7d);
      renderUsers(o.users_7d);
      const when = new Date(o.generated_ms).toLocaleTimeString([], { hour12: false });
      $("stats-note").textContent = `Computed ${when} · refreshes every 5 min`;
    } catch (e) {
      $("stats-note").textContent = `Could not load stats: ${e.message}`;
    }
  }

  function setChannel(id, title) {
    st.channel = id;
    st.channelTitle = title || "";
    const chip = $("channel-filter");
    chip.hidden = !id;
    chip.textContent = id ? `#${st.channelTitle} ✕` : "";
    chip.classList.toggle("active", !!id);
    if (st.overview) renderChannels(st.overview.channels);
    loadDaily();
  }

  function bind() {
    for (const b of document.querySelectorAll("#range .seg-btn")) {
      b.addEventListener("click", () => {
        st.days = Number(b.dataset.days);
        document.querySelectorAll("#range .seg-btn")
          .forEach((x) => x.classList.toggle("active", x === b));
        loadDaily();
      });
    }
    $("channel-filter").addEventListener("click", () => setChannel("", ""));
    $("daily-table-toggle").addEventListener("click", (e) => {
      const table = $("daily-table");
      table.hidden = !table.hidden;
      $("daily-chart").hidden = !table.hidden;
      e.currentTarget.classList.toggle("active", !table.hidden);
      e.currentTarget.textContent = table.hidden ? "Table" : "Chart";
    });
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (!st.active) return;
        if (st.daily.length) renderDaily();
        if (st.overview) renderHourly(st.overview.hourly_utc);
      }, 120);
    });
  }

  function show() {
    st.active = true;
    loadOverview();
    loadDaily();
    clearInterval(st.timer);
    st.timer = setInterval(() => { loadOverview(); loadDaily(); }, STATS_REFRESH_MS);
  }

  function hide() {
    st.active = false;
    clearInterval(st.timer);
    hideTip();
  }

  bind();
  return { show, hide };
})();
