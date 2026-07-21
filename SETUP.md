# Setup & transfer guide

How to move the Gödel research toolkit to another machine (macOS or Linux) and
get the Gödel chat streaming into OpenClaw (or any agent framework).

## Linux notes

`./setup.sh` works on both platforms, but two things differ from macOS:

- **A venv is mandatory, not optional.** Debian/Ubuntu/Mint 24.04+ and Fedora
  ship PEP 668-marked Pythons that refuse `pip install` into the system
  interpreter. `setup.sh` builds `.venv/` (using `uv` if present) and
  `services.sh` prefers it automatically. Run commands as
  `.venv/bin/python -m server.cli ...` or `source .venv/bin/activate` first.
- **The extension loads the same way** in Chrome or Chromium
  (`chrome://extensions` -> Developer mode -> Load unpacked). Snap-packaged
  Chromium is confined and can be awkward about reaching `localhost`; if the
  token never arrives, use the `.deb`/Flatpak build or Chrome proper.

To keep the research PDF corpus on a separate large volume, export these before
running any `research-*` command (see README "Research reports"):

```bash
export GODEL_RESEARCH_DIR=/media/$USER/<DRIVE>/godel_research/research_pdfs
```

## 1. What to transfer

Zip the **parent folder** so the layout is preserved:

```
godel/                         <-- zip this (or the minimal set below)
├── .env                       your keys (Massive, OpenRouter, Schwab)
├── .claude/skills/            the agent skills (13 SKILL.md files)
└── godel_rest/                all the code + the chat database
```

Minimal set if you want a smaller zip: `godel_rest/`, `.claude/skills/`, `.env`
(keep them in one parent folder). The chat history lives in
`godel_rest/godel_rest.db` (~hundreds of MB) — include it to keep all the
analytics/leaderboards; omit it to start the chat archive fresh.

Do **not** rely on `~/.godel_rest/` transferring — those are machine-local tokens
you'll regenerate by logging in (below).

## 2. One-shot setup on the new Mac

```bash
cd godel/godel_rest
./setup.sh
```
Installs Python deps, makes the token dir, and runs the **doctor** which prints a
checklist of what's ready vs. left. Re-run the doctor anytime:
```bash
python3 -m doctor
```

Requires Python 3.11+. The `.env` you copy must contain:
```
MASSIVE_API_KEY="..."        # prices + fundamentals
OPENROUTER_KEY="..."         # LLM labeling + the agent
SCHWAB_APP_KEY="..."         # broker (from developer.schwab.com)
SCHWAB_APP_SECRET="..."
```

## 3. Log into Gödel + start the message stream  ← the important part

The chat feed works in two decoupled pieces:

1. **Token relay** — a Chrome extension sniffs your logged-in Gödel API token and
   hands it to a tiny local receiver. (Gödel's app is behind Cloudflare; the API
   isn't — it just needs your bearer token.)
2. **Chat poller** — uses that token to poll Gödel's REST API and append new
   messages to `godel_rest.db`.

Do this once on the new Mac:

```bash
# a) load the extension
#    Chrome -> chrome://extensions -> enable Developer mode
#    -> Load unpacked -> select  godel_rest/extension

# b) start both services (token receiver + poller)
cd godel/godel_rest
./services.sh start

# c) log into https://app.godelterminal.com in that Chrome and click around once.
#    The extension relays your token; the poller starts saving messages.

./services.sh status     # token present? messages flowing?
./services.sh logs       # live logs
./services.sh stop       # stop the stream
```

The token auto-refreshes as long as you stay logged in (Gödel tokens last ~14
days). If `status` says the token expired, just re-open the Gödel tab.

To auto-start on login, add `cd .../godel_rest && ./services.sh start` to a
launchd plist or your login items (optional).

## 4. How OpenClaw uses this

OpenClaw does **not** stream raw messages itself — that's intentional. The
`services.sh` poller continuously writes new chat to `godel_rest.db`, and the
agent reads from that DB through the **skills**. So "streaming into OpenClaw" =
keep `./services.sh start` running; the agent always sees fresh data.

Wire it up:
1. **Point OpenClaw's skill loader at** `godel/.claude/skills/`. Each skill is a
   `SKILL.md` with YAML frontmatter (`name`, `description`); a generic framework
   injects the name+description index and loads full text on demand. Start skill:
   **`research-toolkit`** (the router).
2. **Give the agent a shell/command tool** whose working directory is
   `godel_rest/`. Restrict it to read-only research commands. A ready-made
   allowlist + a working reference agent are in `godel_rest/agent/`:
   - `agent/tools.py` — the command allowlist (copy its rules into OpenClaw's
     tool, or call it directly). Order/trading commands are blocked.
   - `agent/harness.py` — a complete tool-calling agent (OpenRouter Kimi) you can
     read or reuse.
3. **Verify** the whole loop with the bundled harness (no OpenClaw needed):
   ```bash
   python3 -m agent.run "Research BE end to end, rate it, long/short/pass for my portfolio"
   ```
   ~$0.01, produces `output/BE_thesis.html`.

### If you want the agent to react to NEW messages live
The poller writes monotonic int64 message ids. To trigger the agent on fresh
chat, have OpenClaw poll the high-water mark and run when it advances:
```sql
SELECT MAX(id) FROM chat_messages;          -- compare to last seen
```
Then call `godel-chat-ideas` to surface what's new. (Most use is pull-based: ask
the agent to research something and it queries the live DB.)

## 5. Schwab (for portfolio risk/fit)

```bash
python3 -m broker.setup_auth        # paste your app key/secret + the redirect URL
python3 -m broker.cli auth          # should say authenticated: true
```
Trading is disabled by design — the agent can read your account but cannot place
orders (needs `GODEL_ALLOW_TRADING=1`, which you set only in your own shell).

## 6. Verify everything

```bash
python3 -m doctor                   # all green?
python3 -m tests.test_framework     # 13 deterministic checks
python3 -m analytics.run thesis AAPL  # produces output/AAPL_thesis.html
```

## Optional: re-run / continue chat label upgrade
Direction labels improve with the cheap cloud LLM (Kimi). Resume anytime
(non-destructive, resumable):
```bash
python3 -m analytics.run refresh --classifier hybrid
```
