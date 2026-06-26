# Agent framework — research toolkit for generic agents

This makes the Gödel research stack usable by an autonomous agent (OpenClaw or
any tool-calling framework), and ships a test harness that proves a **no-context**
agent can drive it end to end.

## The skills (the real interface)

Agent-facing capabilities are documented as skills in `godel/.claude/skills/`.
A generic framework injects the skill **index** (name + description) and lets the
agent load full instructions on demand. Start skill: **`research-toolkit`** (the
router). Key skills:

- `research-toolkit` — meta/router; explains the whole toolkit + safety rules.
- `stock-thesis` — full one-ticker report artifact (HTML/MD + charts).
- `financial-statements`, `stock-price-history`, `stock-ratios`,
  `stock-reverse-dcf`, `stock-fundamentals` — granular analysis.
- `portfolio-risk`, `portfolio-fit` — book risk + add-a-name fit (read-only).
- `broker-data` — read-only account/positions/quotes.
- `godel-chat-quant`, `godel-chat-ideas` — chat-derived signal/ideas.
- `autonomous-stock-research` — the end-to-end loop.

Every skill points at a concrete, tested command and states the **no-trading**
rule. Trading is impossible for an agent at two layers: the command allowlist
(harness) and `GODEL_ALLOW_TRADING` env gate (`broker/orders.py`).

## Plugging into your framework

Point the framework's skill loader at `godel/.claude/skills/`. Give the agent a
shell/command tool whose working directory is `godel_rest/`. Restrict it to the
allowlist in `agent/tools.py` (or rely on the order env-gate). The agent reads
skills, runs `python3 -m analytics.run …` / `python3 -m broker.cli …`, reads the
generated `output/*.md`, and reports a rating + recommendation.

## Built-in test harness (validates a no-context agent)

A minimal tool-calling agent driven by a cheap OpenRouter model (default Kimi
`moonshotai/kimi-k2.5`). It receives only the skill index and must discover the
workflow from the docs.

```bash
cd godel_rest
python3 -m agent.run "Research BE end to end, rate it 1-5, and give long/short/pass for my portfolio with risks"
```

Requires `OPENROUTER_KEY` in `godel/.env`. A full run costs ~$0.01 and produces
`output/BE_thesis.html` plus a reasoned rating/recommendation. Tools are
allowlisted (read-only); order commands are blocked.

## Determinism vs judgement

The data pipeline is **deterministic** (same DB/market snapshot → identical
report; verified in `tests/test_framework.py`). The agent adds the **judgement**
layer — a conviction rating and a long/short/pass call — which is the only
non-deterministic part, by design.

## Tests

```bash
python3 -m tests.test_framework      # 13 deterministic checks (no LLM, no cost)
python3 -m agent.run "..."           # LLM end-to-end smoke test (small cost)
```
