#!/usr/bin/env python3
"""Token refresh keeper — run daily via cron to keep Schwab access token alive.

Schwab access tokens last 30 min; refresh tokens last 7 days.
Calling any authenticated endpoint refreshes the access token automatically.
This script does a lightweight call to keep the chain alive.

Run: python3 -m broker.refresh_token
Cron: 0 9 * * * cd ~/.openclaw/workspace/godel_rest && python3 -m broker.refresh_token
"""
import sys
from .tokens import get_token_manager


def main():
    tm = get_token_manager()
    status = tm.status()
    
    if not status["authenticated"]:
        print("ERROR: Not authenticated. Run: python3 -m broker.setup_auth")
        sys.exit(1)
    
    # Force a token refresh if needed by requesting access token
    try:
        access_token = tm.get_access_token()
        new_status = tm.status()
        expires_in = new_status.get("expires_in", 0)
        
        # If refresh token expires in < 48 hours, warn
        if expires_in < 172800:  # 48 hours
            print(f"WARNING: Refresh token expires in {expires_in/3600:.1f} hours!")
            print("ACTION NEEDED: Re-run `python3 -m broker.setup_auth` soon.")
            sys.exit(2)
        
        print(f"OK: Token refreshed. Access token expires in {expires_in/60:.0f} minutes.")
        
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
