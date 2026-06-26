"""Minimal no-context research agent driven by an OpenRouter model (default a
cheap Kimi). It is given only the skill INDEX (as a generic framework would) and
must discover the workflow by reading skills — this validates that the skills are
self-sufficient. Read-only: it cannot trade.

    python3 -m agent.run "Research BE end to end, rate it, and say long/short/pass for my portfolio"
"""
import json
import os

import requests

from analytics.config import _load_env
from . import tools

DEFAULT_MODEL = "moonshotai/kimi-k2.5"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = """You are an autonomous equity-research agent plugged into a tools
framework. You have a set of SKILLS (listed below) and four tools: read_skill,
run_command, read_file, finish.

How to work:
1. Decide which skill fits the task. Start by reading the `research-toolkit`
   skill (the router) with read_skill.
2. Read any specific skills you need, then run their commands with run_command.
3. Read generated reports with read_file (e.g. output/<TICKER>_thesis.md).
4. When done, call finish with: a 1-5 conviction rating, a clear long/short/pass
   recommendation with reasons (fundamentals + portfolio fit), key risks, and the
   path to any artifact you produced.

Hard rules:
- You are READ-ONLY. You cannot place trades. Never attempt order commands.
- Base claims on tool output, not assumptions. Be honest about uncertainty.

Available skills:
%s
"""


def _key() -> str:
    _load_env()
    k = os.environ.get("OPENROUTER_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not k:
        raise RuntimeError("OPENROUTER_KEY missing from godel/.env")
    return k


def _chat(messages, model, key):
    r = requests.post(
        ENDPOINT, headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "tools": tools.TOOL_SPECS,
              "tool_choice": "auto", "temperature": 0}, timeout=120)
    r.raise_for_status()
    return r.json()


def run(task: str, model: str = DEFAULT_MODEL, max_steps: int = 16,
        verbose: bool = True) -> dict:
    key = _key()
    messages = [{"role": "system", "content": SYSTEM % tools.skill_index()},
                {"role": "user", "content": task}]
    transcript, cost = [], 0.0
    for step in range(max_steps):
        data = _chat(messages, model, key)
        cost += data.get("usage", {}).get("cost", 0) or 0
        msg = data["choices"][0]["message"]
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            # model answered in prose without finishing — capture and stop
            return {"answer": msg.get("content", ""), "steps": step + 1,
                    "cost": cost, "transcript": transcript}
        for call in calls:
            fn = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if fn == "finish":
                if verbose:
                    print(f"[agent] FINISH after {step + 1} steps, ${cost:.4f}")
                return {"answer": args.get("answer", ""), "steps": step + 1,
                        "cost": cost, "transcript": transcript}
            result = tools.DISPATCH.get(fn, lambda **_: "unknown tool")(**args)
            if verbose:
                short = (args.get("command") or args.get("name")
                         or args.get("path") or "")
                print(f"[agent] step {step + 1}: {fn}({short}) -> {len(result)} chars")
            transcript.append({"tool": fn, "args": args, "result": result[:500]})
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": result})
    return {"answer": "(max steps reached)", "steps": max_steps, "cost": cost,
            "transcript": transcript}
