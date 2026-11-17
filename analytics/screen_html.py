"""Render a screen result set as a single self-contained, interactive HTML page.

No external assets: the data is embedded as JSON and a small vanilla-JS table
handles click-to-sort (numeric-aware), a text search, and a sector filter. Open
the file directly in any browser — nothing to serve, works offline.
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

# format kinds understood by the client-side formatter (mirror of screen.COLUMNS)
_JS_FORMATTERS = """
function fmt(kind, v) {
  if (v === null || v === undefined || v === "" || (typeof v === 'number' && isNaN(v)))
    return "";
  switch (kind) {
    case "usd": return "$" + abbr(v);
    case "usd_millions": return "$" + abbr(v * 1e6);
    case "mult": return num(v, 1) + "x";
    case "pct": return num(v * 100, 1) + "%";
    case "num": return num(v, 2);
    default: return String(v);
  }
}
function abbr(v) {
  const a = Math.abs(v);
  if (a >= 1e12) return num(v / 1e12, 2) + "T";
  if (a >= 1e9) return num(v / 1e9, 2) + "B";
  if (a >= 1e6) return num(v / 1e6, 2) + "M";
  if (a >= 1e3) return num(v / 1e3, 2) + "K";
  return num(v, 2);
}
function num(v, nd) {
  return v.toLocaleString(undefined, {minimumFractionDigits: nd, maximumFractionDigits: nd});
}
"""


def render(rows: list[dict], errors: list[dict],
           columns: list[tuple[str, str, str]], out_path: str | Path,
           universe_name: str = "S&P 500", period: str = "QTR") -> str:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cols = [{"key": k, "label": lbl, "kind": kind} for k, lbl, kind in columns]
    payload = {"cols": cols, "rows": rows}
    data_json = json.dumps(payload, default=lambda o: None)
    generated = time.strftime("%Y-%m-%d %H:%M")
    err_json = json.dumps(errors)

    page = _TEMPLATE.format(
        title=html.escape(f"{universe_name} reverse-DCF screen"),
        universe=html.escape(universe_name),
        period=html.escape(period),
        generated=generated,
        n_rows=len(rows),
        n_err=len(errors),
        data_json=data_json,
        err_json=err_json,
        js_formatters=_JS_FORMATTERS,
    )
    out_path.write_text(page)
    return str(out_path)


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg:#0f1115; --panel:#171a21; --line:#262b36; --fg:#e7ebf0; --dim:#8b93a3;
    --pos:#3fb950; --neg:#f85149; --accent:#589bff;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
         font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  header {{ padding:16px 20px; border-bottom:1px solid var(--line);
           display:flex; flex-wrap:wrap; gap:12px 20px; align-items:baseline; }}
  h1 {{ font-size:16px; margin:0; font-weight:600; }}
  .meta {{ color:var(--dim); font-size:12px; }}
  .controls {{ margin-left:auto; display:flex; gap:10px; flex-wrap:wrap; }}
  input, select {{ background:var(--panel); color:var(--fg); border:1px solid var(--line);
                  border-radius:6px; padding:6px 9px; font-size:13px; }}
  input::placeholder {{ color:var(--dim); }}
  .wrap {{ overflow-x:auto; }}
  table {{ border-collapse:collapse; width:100%; min-width:1100px; }}
  th, td {{ padding:7px 10px; text-align:right; white-space:nowrap;
           border-bottom:1px solid var(--line); }}
  th:nth-child(1), td:nth-child(1),
  th:nth-child(2), td:nth-child(2),
  th:nth-child(3), td:nth-child(3) {{ text-align:left; }}
  thead th {{ position:sticky; top:0; background:var(--panel); cursor:pointer;
             user-select:none; z-index:1; border-bottom:1px solid var(--line); }}
  thead th:hover {{ color:#fff; }}
  th.sorted::after {{ content:" ▾"; color:var(--accent); }}
  th.sorted.asc::after {{ content:" ▴"; }}
  tbody tr:hover {{ background:#1c2029; }}
  td.ticker {{ font-weight:600; }}
  td.pos {{ color:var(--pos); }}
  td.neg {{ color:var(--neg); }}
  .num-dim {{ color:var(--dim); }}
  footer {{ padding:12px 20px; color:var(--dim); font-size:12px;
           border-top:1px solid var(--line); }}
  details {{ margin-top:6px; }}
  summary {{ cursor:pointer; }}
  code {{ color:var(--dim); }}
</style>
</head>
<body>
<header>
  <h1>{universe} — reverse-DCF screen</h1>
  <span class="meta">{n_rows} names · {period} · generated {generated} · {n_err} errors</span>
  <div class="controls">
    <input id="q" type="search" placeholder="filter ticker / name…" autocomplete="off">
    <select id="sector"><option value="">All sectors</option></select>
  </div>
</header>

<div class="wrap"><table id="t">
  <thead><tr id="head"></tr></thead>
  <tbody id="body"></tbody>
</table></div>

<footer>
  Click a column to sort (click again to reverse). <code>EV/FCF</code> low =
  cheap on cash flow; <code>Implied − Hist</code> = the FCF growth the market
  prices in minus what the company has actually delivered (negative = a low bar).
  Reverse-DCF is not meaningful for banks, insurers and REITs (no clean free
  cash flow) — treat tiny or negative EV/FCF in Financials / Real Estate as noise.
  <details id="errbox"></details>
</footer>

<script>
const DATA = {data_json};
const ERRORS = {err_json};
{js_formatters}

const cols = DATA.cols;
const rows = DATA.rows;
let sortKey = "market_cap", sortAsc = false, sortKind = "usd";

// build header
const head = document.getElementById("head");
cols.forEach(c => {{
  const th = document.createElement("th");
  th.textContent = c.label;
  th.dataset.key = c.key; th.dataset.kind = c.kind;
  th.onclick = () => {{
    if (sortKey === c.key) sortAsc = !sortAsc;
    else {{ sortKey = c.key; sortKind = c.kind; sortAsc = (c.kind === "str"); }}
    draw();
  }};
  head.appendChild(th);
}});

// sector filter options
const sectors = [...new Set(rows.map(r => r.sector).filter(Boolean))].sort();
const sel = document.getElementById("sector");
sectors.forEach(s => {{
  const o = document.createElement("option"); o.value = s; o.textContent = s;
  sel.appendChild(o);
}});

const q = document.getElementById("q");
q.oninput = draw; sel.onchange = draw;

function cmp(a, b) {{
  let x = a[sortKey], y = b[sortKey];
  const isNum = sortKind !== "str";
  const ex = (x === null || x === undefined || x === "");
  const ey = (y === null || y === undefined || y === "");
  if (ex && ey) return 0;
  if (ex) return 1;            // blanks always sink to the bottom
  if (ey) return -1;
  if (isNum) {{ x = +x; y = +y; return sortAsc ? x - y : y - x; }}
  x = String(x).toLowerCase(); y = String(y).toLowerCase();
  return sortAsc ? (x < y ? -1 : x > y ? 1 : 0) : (x < y ? 1 : x > y ? -1 : 0);
}}

function signClass(key, v) {{
  if (v === null || v === undefined || v === "") return "";
  if (key === "growth_gap" || key === "rev_yoy") return v < 0 ? "neg" : "pos";
  return "";
}}

function draw() {{
  const term = q.value.trim().toLowerCase();
  const secf = sel.value;
  let view = rows.filter(r => {{
    if (secf && r.sector !== secf) return false;
    if (!term) return true;
    return (r.ticker || "").toLowerCase().includes(term) ||
           (r.name || "").toLowerCase().includes(term);
  }});
  view.sort(cmp);

  document.querySelectorAll("th").forEach(th => {{
    th.classList.toggle("sorted", th.dataset.key === sortKey);
    th.classList.toggle("asc", th.dataset.key === sortKey && sortAsc);
  }});

  const body = document.getElementById("body");
  body.innerHTML = "";
  const frag = document.createDocumentFragment();
  for (const r of view) {{
    const tr = document.createElement("tr");
    for (const c of cols) {{
      const td = document.createElement("td");
      td.textContent = fmt(c.kind, r[c.key]);
      if (c.key === "ticker") td.className = "ticker";
      else {{
        const sc = signClass(c.key, r[c.key]);
        if (sc) td.className = sc;
        else if (c.kind !== "str" && (r[c.key] === null || r[c.key] === undefined))
          td.className = "num-dim";
      }}
      tr.appendChild(td);
    }}
    frag.appendChild(tr);
  }}
  body.appendChild(frag);
}}

// errors disclosure
const eb = document.getElementById("errbox");
if (ERRORS.length) {{
  eb.innerHTML = "<summary>" + ERRORS.length + " tickers failed</summary>";
  const ul = document.createElement("div");
  ul.style.color = "var(--dim)"; ul.style.marginTop = "6px";
  ul.innerHTML = ERRORS.map(e => e.ticker + " — " + e.error).join("<br>");
  eb.appendChild(ul);
}} else {{ eb.style.display = "none"; }}

draw();
</script>
</body>
</html>
"""
