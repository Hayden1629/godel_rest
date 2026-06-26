"""Schwab broker configuration, loaded from godel/.env.

Required keys:
    SCHWAB_APP_KEY=...
    SCHWAB_APP_SECRET=...
Optional:
    SCHWAB_CALLBACK_URL=https://127.0.0.1      (must match your app registration)
    SCHWAB_TOKEN_FILE=...                       (default: ~/.godel_rest/schwab_tokens.json)
"""
import os
from pathlib import Path

# Reuse the analytics .env loader so a single godel/.env feeds everything.
from analytics.config import _load_env, REPO_ROOT

ENV_PATH = REPO_ROOT / ".env"


def write_env_keys(updates: dict) -> None:
    """Create or update KEY="value" lines in godel/.env, then reload."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    keys = set(updates)
    out = []
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line else None
        if k in keys:
            out.append(f'{k}="{updates[k]}"')
            keys.discard(k)
        else:
            out.append(line)
    for k in keys:  # any not already present
        out.append(f'{k}="{updates[k]}"')
    ENV_PATH.write_text("\n".join(out) + "\n")
    for k, v in updates.items():  # reflect immediately in this process
        os.environ[k] = v

ACCESS_TOKEN_TTL = 1800          # Schwab access tokens last 30 min
REFRESH_BUFFER_SECONDS = 120     # refresh this long before expiry
TRADER_BASE = "https://api.schwabapi.com/trader/v1"
MARKETDATA_BASE = "https://api.schwabapi.com/marketdata/v1"
OAUTH_BASE = "https://api.schwabapi.com/v1/oauth"


def app_key() -> str:
    _load_env()
    key = os.environ.get("SCHWAB_APP_KEY")
    if not key:
        raise RuntimeError("SCHWAB_APP_KEY missing from godel/.env")
    return key


def app_secret() -> str:
    _load_env()
    secret = os.environ.get("SCHWAB_APP_SECRET")
    if not secret:
        raise RuntimeError("SCHWAB_APP_SECRET missing from godel/.env")
    return secret


def callback_url() -> str:
    _load_env()
    return os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")


def token_file() -> Path:
    _load_env()
    override = os.environ.get("SCHWAB_TOKEN_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".godel_rest" / "schwab_tokens.json"
