#!/usr/bin/env python3
"""Schwab token keepalive daemon — runs in background, refreshes token every 25 min.

Schwab access tokens expire after 30 minutes. This daemon makes a lightweight
API call every 25 minutes to keep the token alive indefinitely (until the
7-day refresh token itself expires).

Run via: ./services.sh start (launches alongside token_server and chat_poller)
Or manually: python3 -m broker.keepalive
"""
import sys
import time
import signal
from datetime import datetime

from .tokens import get_token_manager
from .accounts import AccountClient

# Refresh interval: 25 minutes (token expires at 30 min)
REFRESH_INTERVAL = 25 * 60  # seconds
RUNNING = True


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def refresh_loop():
    tm = get_token_manager()
    client = AccountClient()
    
    while RUNNING:
        try:
            # Make a lightweight API call to trigger token refresh if needed
            # balances() is lightweight and always works
            bal = client.balances()
            
            status = tm.status()
            expires_in = status.get("expires_in", 0)
            
            if status["authenticated"]:
                log(f"Token alive. Expires in {expires_in//60} min.")
            else:
                log("ERROR: Token not authenticated!")
                
        except Exception as e:
            log(f"ERROR refreshing token: {e}")
        
        # Sleep in chunks so we respond to signals quickly
        slept = 0
        while RUNNING and slept < REFRESH_INTERVAL:
            time.sleep(1)
            slept += 1


def signal_handler(signum, frame):
    global RUNNING
    log(f"Received signal {signum}, shutting down...")
    RUNNING = False


def main():
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    log("Schwab token keepalive daemon starting...")
    log(f"Refresh interval: {REFRESH_INTERVAL//60} minutes")
    
    # Do an immediate check on startup
    try:
        tm = get_token_manager()
        status = tm.status()
        if not status["authenticated"]:
            log("ERROR: Not authenticated. Run: python3 -m broker.setup_auth")
            sys.exit(1)
        log(f"Initial token status: expires in {status.get('expires_in', 0)//60} min")
    except Exception as e:
        log(f"ERROR on startup: {e}")
        sys.exit(1)
    
    refresh_loop()
    log("Daemon exited.")


if __name__ == "__main__":
    main()
