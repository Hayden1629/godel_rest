"""Buttons -> commands. Process control goes through services.sh so the GUI and
the shell share one source of truth (same pid files, same logs, same flags).
Only the names in ACTIONS can run; nothing from the request reaches a shell."""
import subprocess

from .state import ROOT

SERVICES_SH = ROOT / "services.sh"
ACTION_TIMEOUT_S = 30

# GUI action -> services.sh subcommand
ACTIONS = {
    "start": "start",                  # token relay + chat poller
    "stop": "stop",
    "backfill": "backfill",            # backfill --gap
    "backfill-stop": "backfill-stop",
}


def run(action: str) -> dict:
    if action == "check-token":
        return _check_token()
    sub = ACTIONS.get(action)
    if sub is None:
        return {"ok": False, "output": "", "error": f"unknown action {action!r}"}
    try:
        r = subprocess.run([str(SERVICES_SH), sub], cwd=ROOT, capture_output=True,
                           text=True, timeout=ACTION_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "",
                "error": f"services.sh {sub} timed out after {ACTION_TIMEOUT_S}s"}
    except OSError as e:
        return {"ok": False, "output": "", "error": f"could not run services.sh: {e}"}
    output = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        return {"ok": False, "output": output,
                "error": f"services.sh {sub} exited {r.returncode}"}
    return {"ok": True, "output": output, "error": None}


def _check_token() -> dict:
    """Run the ntfy expiry check now (it alerts only inside the warning window)."""
    try:
        from server import token_watch
    except ImportError:
        return {"ok": False, "output": "",
                "error": "server.token_watch not found (it ships on main)"}
    try:
        return {"ok": True, "output": token_watch.check(), "error": None}
    except Exception as e:
        return {"ok": False, "output": "", "error": f"expiry check failed: {e}"}
