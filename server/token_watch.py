"""Pushes an ntfy.sh alert when the captured Godel token is close to expiring,
so the chat poller/backfill don't just silently start failing.

    python3 -m server.token_watch

Meant to run periodically (cron/launchd). Notifies once per distinct token
expiry (tracked in ~/.godel_rest/notify_state.json) so it doesn't re-alert on
every tick once you're inside the warning window.
"""
import json
import time

import requests

from . import token_store

NTFY_URL = "https://ntfy.sh/hayden-website-updates"
WARN_WINDOW_S = 24 * 3600
STATE_PATH = token_store.STORE_DIR / "notify_state.json"


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def notify(title: str, message: str, priority: str = "default") -> bool:
    try:
        resp = requests.post(NTFY_URL, data=message.encode("utf-8"),
                              headers={"Title": title, "Priority": priority}, timeout=10)
        return resp.ok
    except Exception:
        return False


def check() -> str:
    rec = token_store.load()
    if not rec or not rec.get("exp"):
        return "no token captured -- nothing to check"
    exp = rec["exp"]
    secs = token_store.seconds_until_expiry()
    state = _load_state()
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))

    if state.get("last_notified_exp") == exp:
        return f"already notified for this token (exp {when}), skipping"

    if secs is not None and secs <= 0:
        ok = notify("Godel token EXPIRED",
                     f"Token expired {when}. Re-open app.godelterminal.com "
                     "(logged in) to refresh it.", priority="urgent")
        if ok:
            _save_state({**state, "last_notified_exp": exp})
        return f"expired -- notify {'sent' if ok else 'FAILED'}"

    if secs is not None and secs <= WARN_WINDOW_S:
        hours = secs / 3600
        ok = notify("Godel token expiring soon",
                     f"Expires in {hours:.1f}h ({when}). Re-open "
                     "app.godelterminal.com (logged in) to refresh before the "
                     "chat poller/backfill start failing.", priority="high")
        if ok:
            _save_state({**state, "last_notified_exp": exp})
        return f"expiring in {hours:.1f}h -- notify {'sent' if ok else 'FAILED'}"

    return f"ok, {secs/3600:.1f}h left" if secs is not None else "ok, no expiry set"


if __name__ == "__main__":
    print(check())
