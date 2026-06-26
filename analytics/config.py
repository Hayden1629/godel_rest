"""Shared config for the analytics layer: DB path + Massive API key."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # godel_rest/
DB_PATH = ROOT / "godel_rest.db"
REPO_ROOT = ROOT.parent                                 # godel/

# Forward-return horizons in trading days.
HORIZONS = (1, 5, 20)
MARKET_BENCHMARK = "SPY"


def _load_env() -> None:
    """Populate os.environ from the nearest .env (repo root takes priority)."""
    for env_path in (REPO_ROOT / ".env", ROOT / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def massive_api_key() -> str:
    _load_env()
    key = os.environ.get("MASSIVE_API_KEY")
    if not key:
        raise RuntimeError(
            "MASSIVE_API_KEY not found. Add it to godel/.env as "
            'MASSIVE_API_KEY="..."'
        )
    return key
