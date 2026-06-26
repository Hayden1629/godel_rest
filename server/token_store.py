"""Shared Bearer-token persistence between the extension receiver and the
pollers. The token lives in a single JSON file so any process can read the
freshest value the browser has produced."""
import base64
import json
import time
from pathlib import Path
from typing import Optional

STORE_DIR = Path.home() / ".godel_rest"
STORE_PATH = STORE_DIR / "token.json"


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_exp(jwt: str) -> Optional[int]:
    """Return the JWT `exp` claim (unix seconds) or None if undecodable."""
    try:
        payload = json.loads(_b64url_decode(jwt.split(".")[1]))
        return int(payload["exp"])
    except Exception:
        return None


def decode_claims(jwt: str) -> dict:
    try:
        return json.loads(_b64url_decode(jwt.split(".")[1]))
    except Exception:
        return {}


def save(token: str, exp: Optional[int] = None) -> dict:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "token": token,
        "exp": exp if exp is not None else decode_exp(token),
        "captured_at": int(time.time()),
    }
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(STORE_PATH)
    return record


def load() -> Optional[dict]:
    if not STORE_PATH.exists():
        return None
    try:
        return json.loads(STORE_PATH.read_text())
    except Exception:
        return None


def load_token() -> str:
    """Return the current token or raise with a clear, actionable message."""
    rec = load()
    if not rec or not rec.get("token"):
        raise RuntimeError(
            "No token captured yet. Start `python -m server.token_server`, "
            "load the extension, and log in at app.godelterminal.com."
        )
    exp = rec.get("exp")
    if exp and exp < time.time():
        raise RuntimeError(
            "Captured token is expired. Re-open app.godelterminal.com (logged "
            "in) so the extension relays a fresh token."
        )
    return rec["token"]


def seconds_until_expiry() -> Optional[int]:
    rec = load()
    if not rec or not rec.get("exp"):
        return None
    return int(rec["exp"] - time.time())
