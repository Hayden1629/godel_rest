"""Schwab OAuth token storage + refresh.

Schwab access tokens live 30 minutes; refresh tokens live 7 days. We persist
both and refresh lazily on demand (no background thread to leak). When the
refresh token itself expires you must re-run the one-time `setup_auth` flow.
"""
import base64
import json
import os
import stat
import threading
import time
from typing import Optional

import requests

from . import config


def _save(token_response: dict) -> dict:
    """Persist a raw Schwab token response, stamping an absolute expiry."""
    path = config.token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "access_token": token_response.get("access_token"),
        "refresh_token": token_response.get("refresh_token"),
        "id_token": token_response.get("id_token"),
        "token_type": token_response.get("token_type", "Bearer"),
        "expires_at": time.time() + token_response.get("expires_in",
                                                       config.ACCESS_TOKEN_TTL),
        "updated_at": time.time(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    tmp.replace(path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return record


def _load() -> Optional[dict]:
    path = config.token_file()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _basic_auth_header() -> dict:
    creds = f"{config.app_key()}:{config.app_secret()}"
    encoded = base64.b64encode(creds.encode()).decode()
    return {"Authorization": f"Basic {encoded}",
            "Content-Type": "application/x-www-form-urlencoded"}


def exchange_code(auth_code: str) -> dict:
    """Exchange an authorization code for tokens (used by setup_auth)."""
    resp = requests.post(
        f"{config.OAUTH_BASE}/token",
        headers=_basic_auth_header(),
        data={"grant_type": "authorization_code", "code": auth_code,
              "redirect_uri": config.callback_url()},
        timeout=30)
    resp.raise_for_status()
    return _save(resp.json())


def _refresh(refresh_token: str) -> dict:
    resp = requests.post(
        f"{config.OAUTH_BASE}/token",
        headers=_basic_auth_header(),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Schwab token refresh failed ({resp.status_code}): {resp.text}. "
            "The refresh token may be expired — re-run `python -m broker.setup_auth`.")
    return _save(resp.json())


class TokenManager:
    """Thread-safe access-token provider with lazy refresh."""
    _instance = None
    _new_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._new_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._lock = threading.Lock()
        return cls._instance

    def get_access_token(self) -> str:
        with self._lock:
            rec = _load()
            if not rec or not rec.get("refresh_token"):
                raise RuntimeError(
                    "No Schwab tokens found. Run `python -m broker.setup_auth` once.")
            if time.time() >= rec["expires_at"] - config.REFRESH_BUFFER_SECONDS:
                rec = _refresh(rec["refresh_token"])
            return rec["access_token"]

    def status(self) -> dict:
        rec = _load()
        if not rec:
            return {"authenticated": False}
        return {
            "authenticated": True,
            "expires_in": round(rec["expires_at"] - time.time()),
            "updated_at": time.strftime("%Y-%m-%d %H:%M",
                                        time.localtime(rec.get("updated_at", 0))),
        }


def get_token_manager() -> TokenManager:
    return TokenManager()
