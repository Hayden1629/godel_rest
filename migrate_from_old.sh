#!/usr/bin/env bash
# One-shot migration: move the live Gödel chat DB from the old repo into the
# OpenClaw workspace copy, rebuild the venv, and restart the streaming services
# from the new location. Run ON THE MAC:
#   bash ~/.openclaw/workspace/godel_rest/migrate_from_old.sh
set -euo pipefail

OLD="$HOME/godel_rest"
NEW="$HOME/.openclaw/workspace/godel_rest"

echo "== 1/5 Stopping old services (token relay + chat poller) =="
if [ -x "$OLD/services.sh" ]; then (cd "$OLD" && ./services.sh stop || true); fi

echo "== 2/5 Moving databases =="
# Move (not copy) so there's exactly one live DB. -wal/-shm move with it.
for f in godel_rest.db godel_rest.db-wal godel_rest.db-shm; do
  [ -f "$OLD/$f" ] && mv "$OLD/$f" "$NEW/$f" && echo "  moved $f"
done
# research.db was already copied; prefer the newer one if the old repo's changed
if [ -f "$OLD/research.db" ] && [ "$OLD/research.db" -nt "$NEW/research.db" ]; then
  cp "$OLD/research.db" "$NEW/research.db" && echo "  refreshed research.db"
fi

echo "== 3/5 Locating .env (Massive/OpenRouter/Schwab keys) =="
# Loader checks workspace/.env then godel_rest/.env. Old layout: godel/.env = ~/.env
for cand in "$OLD/.env" "$HOME/.env"; do
  if [ -f "$cand" ] && [ ! -f "$HOME/.openclaw/workspace/.env" ]; then
    cp "$cand" "$HOME/.openclaw/workspace/.env" && echo "  copied $cand -> workspace/.env"
    break
  fi
done
[ -f "$HOME/.openclaw/workspace/.env" ] || echo "  WARNING: no .env found — create workspace/.env with MASSIVE_API_KEY, OPENROUTER_KEY, SCHWAB_APP_KEY/SECRET"

echo "== 4/5 Rebuilding venv in new location =="
(cd "$NEW" && ./setup.sh)

echo "== 5/5 Starting services from workspace =="
(cd "$NEW" && ./services.sh start && sleep 2 && ./services.sh status)

echo
echo "Done. Chrome extension keeps working as-is (it talks to 127.0.0.1:8765,"
echo "not a path). Optionally re-load it later from $NEW/extension."
echo "Old repo left at $OLD (minus the DB) — delete it once you're happy."
echo "If the poller missed messages during the switch:"
echo "  cd $NEW && .venv/bin/python -m server.cli backfill --channels all --max-pages 2"
