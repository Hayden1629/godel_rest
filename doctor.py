"""Setup health check. Run on the new machine to see what's ready and what's
left:

    python3 -m doctor
"""
import importlib
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OK, WARN, BAD = "  ✓", "  •", "  ✗"


def _env():
    from analytics.config import _load_env
    _load_env()


def check_deps():
    missing = []
    for mod in ("requests", "pandas", "numpy", "scipy", "sklearn", "networkx",
                "statsmodels", "openpyxl", "markdown", "matplotlib"):
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)
    if missing:
        print(f"{BAD} python deps missing: {', '.join(missing)}")
        print("      fix: pip install -r requirements.txt")
        return False
    print(f"{OK} python dependencies installed")
    return True


def check_env():
    _env()
    keys = {"MASSIVE_API_KEY": "prices/fundamentals",
            "OPENROUTER_KEY": "LLM labeling + agent",
            "SCHWAB_APP_KEY": "broker", "SCHWAB_APP_SECRET": "broker"}
    ok = True
    for k, what in keys.items():
        present = bool(os.environ.get(k) or
                       (k == "OPENROUTER_KEY" and os.environ.get("OPENROUTER_API_KEY")))
        print(f"{OK if present else BAD} .env {k} ({what})"
              + ("" if present else "  — add to godel/.env"))
        ok = ok and present
    return ok


def check_godel_token():
    from server import token_store
    rec = token_store.load()
    if not rec:
        print(f"{BAD} Gödel token not captured")
        print("      fix: python3 -m server.token_server  (then load extension + log in)")
        return False
    secs = token_store.seconds_until_expiry()
    if secs and secs < 0:
        print(f"{WARN} Gödel token expired — re-open app.godelterminal.com (logged in)")
        return False
    print(f"{OK} Gödel token present ({secs // 3600 if secs else '?'}h left)")
    return True


def check_schwab():
    try:
        from broker.tokens import get_token_manager
        st = get_token_manager().status()
    except Exception as e:
        print(f"{WARN} Schwab not configured ({e})")
        return False
    if not st.get("authenticated"):
        print(f"{BAD} Schwab not authenticated")
        print("      fix: python3 -m broker.setup_auth")
        return False
    print(f"{OK} Schwab authenticated (token {st['expires_in']}s)")
    return True


def check_db():
    db = ROOT / "godel_rest.db"
    if not db.exists():
        print(f"{WARN} chat database not present (godel_rest.db) — start the poller "
              "to build it, or copy it over")
        return False
    conn = sqlite3.connect(str(db))
    try:
        msgs = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
        ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        print(f"{OK} chat database: {msgs:,} messages, {ev:,} events")
    except Exception:
        print(f"{WARN} chat database present but empty/uninitialized")
    conn.close()
    return True


def main():
    print("Gödel research toolkit — setup doctor\n")
    results = {
        "deps": check_deps(),
        "env": check_env(),
        "godel": check_godel_token(),
        "schwab": check_schwab(),
        "db": check_db(),
    }
    print()
    if all(results.values()):
        print("All green — run:  python3 -m tests.test_framework")
    else:
        todo = [k for k, v in results.items() if not v]
        print(f"Not ready yet: {', '.join(todo)} (see fixes above). "
              "See SETUP.md for the full walkthrough.")


if __name__ == "__main__":
    main()
