"""One-time interactive Schwab OAuth setup — a single paste-driven script.

    python3 -m broker.setup_auth

Prompts for your Schwab app key/secret (from the developer portal) if they are
not already in godel/.env and saves them, then opens the authorize page. You log
in, approve, and paste the full redirect URL (contains ?code=...); we exchange it
for tokens. Re-run only when the 7-day refresh token has expired.
"""
import os
import re
import webbrowser
from urllib.parse import unquote

from analytics.config import _load_env

from . import config, tokens


def ensure_credentials() -> None:
    """Make sure the app key/secret (and callback) are in .env, prompting if not."""
    _load_env()
    updates = {}
    if not os.environ.get("SCHWAB_APP_KEY"):
        updates["SCHWAB_APP_KEY"] = input("Schwab APP KEY: ").strip()
    if not os.environ.get("SCHWAB_APP_SECRET"):
        updates["SCHWAB_APP_SECRET"] = input("Schwab APP SECRET: ").strip()
    if not os.environ.get("SCHWAB_CALLBACK_URL"):
        cb = input("Callback URL [https://127.0.0.1]: ").strip()
        if cb:
            updates["SCHWAB_CALLBACK_URL"] = cb
    if updates:
        config.write_env_keys(updates)
        print(f"Saved {', '.join(updates)} to {config.ENV_PATH}")


def authorize_url() -> str:
    return (f"{config.OAUTH_BASE}/authorize"
            f"?client_id={config.app_key()}"
            f"&redirect_uri={config.callback_url()}")


def extract_code(redirect_url: str) -> str:
    m = re.search(r"[?&]code=([^&]+)", redirect_url)
    if not m:
        raise ValueError("No ?code= found in the pasted URL.")
    return unquote(m.group(1))


def main():
    ensure_credentials()
    url = authorize_url()
    print("Opening Schwab authorization page…")
    print(url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("\nAfter approving, your browser redirects to a URL like "
          f"{config.callback_url()}/?code=...")
    redirect_url = input("Paste that full redirect URL here:\n").strip()
    code = extract_code(redirect_url)
    rec = tokens.exchange_code(code)
    print(f"\n✓ Authenticated. Access token expires in "
          f"{round(rec['expires_at'] - __import__('time').time())}s; "
          f"refresh token valid ~7 days.")


if __name__ == "__main__":
    main()
