"""Earnings-call transcripts (TRAN) — fetch + parse.

Discovered from HAR captures 2026-08-07 (see docs in earnings-transcripts skill):

    GET /api/news/file-content/{provider}/{TICKER}/{year}/{uuid}.xml

returns an XML document (provider: alphastreet) whose <htmlBody> holds the
transcript as HTML: header (company/quarter/date), participants, then speaker
turns — `speaker-details` divs mark the speaker, `content-para` paragraphs the
words. The LIST endpoint (ticker -> available transcript paths) is still
captured 2026-08-07 (tran_command4.har):

    POST /api/news/items
    {"sources": ["directory:recommended:Transcripts"],
     "dateTimeRange": {"startTime": 0, "endTime": <now_ms>},
     "legalEntityIds": [<id>], "pageable": {"size": 200}}

legalEntityId resolves ticker -> seriesId -> company-profile, the same chain FA
uses (analytics.financials.resolve_entity). Items carry feedId + filename;
document URL = /api/news/file-content/{feedId}/{filename}.

Parsing is lenient (stdlib HTMLParser): document structure varies between
files (some embed base64 images, speaker roles appear as <div> or <p>).
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "output" / "transcripts"

_PATH_RE = re.compile(
    r"/api/news/file-content/(?P<provider>[\w-]+)/(?P<ticker>[\w.]+)/"
    r"(?P<year>\d{4})/(?P<uuid>[\w-]+)\.xml")


class _T(HTMLParser):
    """Streams the transcript HTML into (speaker, role, [paragraphs]) turns."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.header: dict = {}
        self.participants: list[dict] = []
        self.turns: list[dict] = []       # {speaker, role, paras: []}
        self._stack: list[tuple[str, str]] = []   # (tag, class)
        self._buf: list[str] | None = None
        self._cur = {"speaker": "Operator", "role": "", "paras": []}
        self._in_speaker = False
        self._speaker_parts: list[str] = []
        self._in_participants = False
        self._pname: str | None = None

    def _cls(self, attrs) -> str:
        return dict(attrs).get("class", "") or ""

    def handle_starttag(self, tag, attrs):
        c = self._cls(attrs)
        self._stack.append((tag, c))
        if tag == "img":
            self._stack.pop()  # img is void; ignore entirely
            return
        if "speaker-details" in c:
            self._flush_turn()
            self._in_speaker = True
            self._speaker_parts = []
        if "participants-content" in c:
            self._in_participants = True
        # text-collecting elements
        if tag in ("h2", "h3", "h6", "p", "div") and self._buf is None:
            if ("content-para" in c or self._in_speaker or self._in_participants
                    or "sub-title" in c or "date-time" in c
                    or "transcript-type" in c or tag in ("h2", "h3", "h6")):
                self._buf = []

    _INLINE = {"span", "a", "strong", "em", "b", "i", "u", "br", "sup", "sub"}

    def handle_endtag(self, tag):
        if self._buf is not None and tag in self._INLINE:
            # inline tag inside a collecting element — keep collecting
            for i in range(len(self._stack) - 1, -1, -1):
                if self._stack[i][0] == tag:
                    del self._stack[i]
                    break
            return
        # find matching open tag
        cls = ""
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                cls = self._stack[i][1]
                del self._stack[i]
                break
        if self._buf is None:
            if tag == "div" and self._in_speaker and "speaker-details" in cls:
                self._end_speaker()
            if tag == "div" and "participants-content" in cls:
                self._in_participants = False
                self._pname = None
            return
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = None
        if not text:
            pass
        elif tag == "h2" and "company" not in self.header:
            m = re.match(r"(.+?)\s*\(([\w.]+)\)\s*$", text)
            self.header["company"] = (m.group(1) if m else text).strip()
            if m:
                self.header["ticker"] = m.group(2)
        elif "sub-title" in cls:
            self.header["event"] = text
        elif "date-time" in cls:
            self.header["date"] = text
        elif "transcript-type" in cls:
            self.header["type"] = text
        elif self._in_speaker and tag in ("h6", "h3"):
            self._speaker_parts.insert(0, text)      # name
        elif self._in_speaker and tag in ("div", "p"):
            self._speaker_parts.append(text)         # role line
        elif self._in_participants and tag in ("h3", "h6"):
            if "title" in cls:
                self._pname = None                   # group heading
            else:
                self._pname = text
                self.participants.append({"name": text, "role": ""})
        elif self._in_participants and tag in ("div", "p") and self._pname:
            if self.participants and not self.participants[-1]["role"]:
                self.participants[-1]["role"] = text
        elif "content-para" in cls:
            self._cur["paras"].append(text)
        elif tag == "h3" and "title" in cls:
            self._flush_turn()
            self.turns.append({"speaker": f"— {text} —", "role": "", "paras": []})
            self._cur = {"speaker": "Operator", "role": "", "paras": []}

        if tag == "div" and self._in_speaker and "speaker-details" in cls:
            self._end_speaker()

    def _end_speaker(self):
        self._in_speaker = False
        parts = [p for p in self._speaker_parts if p]
        self._cur = {"speaker": parts[0] if parts else "Unknown",
                     "role": parts[1] if len(parts) > 1 else "", "paras": []}

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def _flush_turn(self):
        if self._cur["paras"]:
            self.turns.append(self._cur)
        self._cur = dict(self._cur, paras=[])


def parse(xml_text: str) -> dict:
    p = _T()
    p.feed(xml_text)
    p._flush_turn()
    turns = [t for t in p.turns if t["paras"] or t["speaker"].startswith("—")]
    return {"header": p.header, "participants": p.participants, "turns": turns}


def to_text(doc: dict) -> str:
    h = doc["header"]
    lines = [f"{h.get('company', '?')} ({h.get('ticker', '?')}) — "
             f"{h.get('event', '')} — {h.get('date', '')}", "=" * 72]
    for t in doc["turns"]:
        if t["speaker"].startswith("—"):
            lines += ["", t["speaker"], ""]
            continue
        who = t["speaker"] + (f" [{t['role']}]" if t["role"] else "")
        lines += ["", who + ":"] + ["  " + p for p in t["paras"]]
    return "\n".join(lines)


def list_transcripts(ticker: str) -> list[dict]:
    """All earnings-call transcripts available for a ticker, newest first."""
    import time as _time
    from . import api_client
    from analytics.financials import resolve_entity
    ent = resolve_entity(ticker)
    body = {"sources": ["directory:recommended:Transcripts"],
            "dateTimeRange": {"startTime": 0, "endTime": int(_time.time() * 1000)},
            "legalEntityIds": [int(ent["legal_entity_id"])],
            "pageable": {"size": 200}}
    res = api_client.post("/api/news/items", body)
    out = []
    for it in res.get("content") or []:
        fn = it.get("filename")
        if not fn:
            continue
        ts = int(it.get("publicationTime") or 0) / 1000
        out.append({
            "title": it.get("title"),
            "date": _time.strftime("%Y-%m-%d", _time.gmtime(ts)) if ts else None,
            "path": f"/api/news/file-content/{it.get('feedId') or 'alphastreet'}/{fn}",
        })
    return out


def _saved_sources() -> set[str]:
    srcs = set()
    for j in OUT_DIR.glob("*.json"):
        try:
            s = json.loads(j.read_text()).get("source") or ""
            m = _PATH_RE.search(s)
            if m:
                srcs.add(m.group(0))
        except Exception:
            continue
    return srcs


def fetch_all(ticker: str, skip_existing: bool = True) -> list[dict]:
    """Fetch + save every transcript for a ticker. Returns per-item status."""
    have = _saved_sources() if skip_existing else set()
    results = []
    for item in list_transcripts(ticker):
        if item["path"] in have:
            results.append({**item, "status": "already saved"})
            continue
        try:
            doc = fetch_parsed(item["path"], save=True)
            results.append({**item, "status": "saved", "file": doc.get("saved")})
        except Exception as e:  # keep going; report per-item
            results.append({**item, "status": f"ERROR: {e}"})
    return results


def fetch(path_or_url: str) -> str:
    """GET the raw transcript XML (auth via the relayed token)."""
    from . import api_client
    import requests, time
    url = (path_or_url if path_or_url.startswith("http")
           else f"{api_client.BASE}{path_or_url}")
    start = time.perf_counter()
    status: int | str = "ERR"
    try:
        r = requests.get(url, headers=api_client._headers(), timeout=30)
        status = r.status_code
        api_client._check(r, path_or_url)
        return r.text
    finally:
        api_client.record("GET", path_or_url, None, status,
                          (time.perf_counter() - start) * 1000)


def fetch_parsed(path_or_url: str, save: bool = True) -> dict:
    xml_text = fetch(path_or_url)
    doc = parse(xml_text)
    if save:
        doc["saved"] = str(save_doc(doc, xml_text, path_or_url))
    return doc


def save_doc(doc: dict, xml_text: str | None = None,
             src: str | None = None) -> Path:
    """Write .txt (+ raw .xml) under output/transcripts/, return txt path."""
    h = doc["header"]
    tick = h.get("ticker") or "UNKNOWN"
    if not h.get("ticker") and src:
        m = _PATH_RE.search(src)
        if m:
            tick = m.group("ticker")
    slug = re.sub(r"[^\w]+", "_", (h.get("event") or "transcript")).strip("_")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = OUT_DIR / f"{tick}_{slug}"
    base.with_suffix(".txt").write_text(to_text(doc))
    meta = {"source": src, **h}
    base.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    if xml_text:
        base.with_suffix(".xml").write_text(xml_text)
    return base.with_suffix(".txt")
