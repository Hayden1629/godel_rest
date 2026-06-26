#!/usr/bin/env bash
# Gödel streaming services: the token relay + the chat poller. Together they keep
# godel_rest.db continuously updated with new chat messages — which is what
# OpenClaw (or any agent) reads from via the godel-chat-* skills.
#
#   ./services.sh start      # start token receiver + chat poller
#   ./services.sh stop
#   ./services.sh status
#   ./services.sh logs       # tail both logs
#
# Channels can be overridden:  CHANNELS=general,biotech ./services.sh start

set -euo pipefail
cd "$(dirname "$0")"
RUN=run
mkdir -p "$RUN"
PY=${PY:-python3}
CHANNELS=${CHANNELS:-general,biotech,options,quant,paid}

_pid()   { [ -f "$RUN/$1.pid" ] && cat "$RUN/$1.pid" || echo ""; }
_alive() { local p; p=$(_pid "$1"); [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }

start_one() {
  local name=$1; shift
  if _alive "$name"; then echo "  $name already running (pid $(_pid "$name"))"; return; fi
  nohup "$PY" "$@" > "$RUN/$name.log" 2>&1 &
  echo $! > "$RUN/$name.pid"
  echo "  started $name (pid $!) -> $RUN/$name.log"
}

case "${1:-status}" in
  start)
    echo "Starting Gödel streaming services…"
    start_one token_server -m server.token_server
    sleep 1
    start_one chat_poller  -m server.cli chat --channels "$CHANNELS"
    echo "Done. Load the Chrome extension + log into app.godelterminal.com so the"
    echo "token relays. Check: ./services.sh status"
    ;;
  stop)
    for name in chat_poller token_server; do
      if _alive "$name"; then kill "$(_pid "$name")" && echo "  stopped $name"; fi
      rm -f "$RUN/$name.pid"
    done
    ;;
  status)
    for name in token_server chat_poller; do
      if _alive "$name"; then echo "  $name: RUNNING (pid $(_pid "$name"))";
      else echo "  $name: stopped"; fi
    done
    "$PY" - <<'PY'
from server import token_store
import sqlite3, time
rec = token_store.load()
if rec:
    secs = (rec.get("exp") or 0) - time.time()
    print(f"  token: present ({secs/3600:.1f}h left)")
else:
    print("  token: NONE (load extension + log into Gödel)")
try:
    c = sqlite3.connect("godel_rest.db")
    n, last = c.execute("SELECT COUNT(*), MAX(created_at) FROM chat_messages").fetchone()
    print(f"  db: {n:,} messages, newest {last}")
except Exception:
    print("  db: not initialized yet")
PY
    ;;
  logs) tail -n 40 -f "$RUN"/token_server.log "$RUN"/chat_poller.log ;;
  *) echo "usage: ./services.sh {start|stop|status|logs}"; exit 1 ;;
esac
