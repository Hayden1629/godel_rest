"""Local HTTP receiver. The browser extension POSTs the captured Bearer token
here; we persist it via token_store. Stdlib only — no extra deps, binds to
loopback only so it is never exposed off-machine."""
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import token_store

HOST = "127.0.0.1"
PORT = 8765
ENDPOINTS_PATH = token_store.STORE_DIR / "endpoints.log"      # human-readable
ENDPOINTS_JSONL = token_store.STORE_DIR / "endpoints.jsonl"   # full detail
SAMPLES_DIR = token_store.STORE_DIR / "samples"               # response bodies
_seen_endpoints: set[str] = set()


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_")[:80] or "root"


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            rec = token_store.load()
            if not rec:
                return self._json(200, {"captured": False})
            exp = rec.get("exp")
            return self._json(
                200,
                {
                    "captured": True,
                    "exp": exp,
                    "seconds_until_expiry": (exp - int(time.time())) if exp else None,
                    "captured_at": rec.get("captured_at"),
                },
            )
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/observe":
            return self._observe()
        if self.path != "/token":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            token = data.get("token")
            if not token:
                return self._json(400, {"error": "missing token"})
            rec = token_store.save(token, data.get("exp"))
            exp = rec.get("exp")
            exp_txt = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) if exp else "?"
            print(f"[token_server] token received, expires {exp_txt}", flush=True)
            return self._json(200, {"ok": True, "exp": exp})
        except Exception as e:
            return self._json(400, {"error": str(e)})

    def _observe(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            command = data.get("command") or "-"
            method = data.get("method", "GET")
            path = data.get("path", "")
            status = data.get("status")
            # dedupe on (command, method, path) so the same endpoint under a new
            # command is still recorded, and repeats don't spam the log
            key = f"{command} {method} {path}"
            if key in _seen_endpoints:
                return self._json(200, {"ok": True, "duplicate": True})
            _seen_endpoints.add(key)
            token_store.STORE_DIR.mkdir(parents=True, exist_ok=True)

            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            status_txt = f"{status}" if status is not None else "?"
            # human-readable line: when | command | status | METHOD path | example
            with open(ENDPOINTS_PATH, "a") as f:
                f.write(f"{ts}\t{command}\t{status_txt}\t{method} {path}\t"
                        f"{data.get('example','')}\n")

            # save the response sample to its own file for shape inspection
            sample = data.get("sample")
            sample_file = None
            if sample:
                SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
                sample_file = SAMPLES_DIR / f"{_slug(command)}__{method}__{_slug(path)}.json"
                sample_file.write_text(sample)

            with open(ENDPOINTS_JSONL, "a") as f:
                f.write(json.dumps({
                    "ts": ts, "command": command, "method": method,
                    "path": path, "query": data.get("query"), "status": status,
                    "example": data.get("example"),
                    "sample_file": str(sample_file) if sample_file else None,
                }) + "\n")

            flag = "" if str(status) == "200" else f"  <- status {status_txt}"
            print(f"[discover] {command:<22} {method:<5} {path}{flag}", flush=True)
            return self._json(200, {"ok": True})
        except Exception as e:
            return self._json(400, {"error": str(e)})

    def log_message(self, *args):
        pass  # silence default per-request logging


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[token_server] listening on http://{HOST}:{PORT}", flush=True)
    print("[token_server] waiting for the extension to relay a token…", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[token_server] stopped", flush=True)


if __name__ == "__main__":
    main()
