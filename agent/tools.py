"""Sandboxed tools the agent may call.

Trading model (changed 2026-08-13): the agent may PROPOSE a trade and, once
Hayden replies YES to that proposal in chat, CONFIRM it. It may not place an
order by any other route. `broker.cli order`, `--confirm`, setup_auth and the
GODEL_ALLOW_TRADING variable stay blocked here: those are Hayden's manual
paths, and an agent that can reach them could trade without a code.

Read commands are confined to the project tree. Confirmation codes, the ticket
store and the audit log live in ~/.openclaw/ specifically so that this sandbox
cannot read them; a `cat` that escapes GODEL_ROOT would undo that.
"""
import re
import subprocess
from pathlib import Path

GODEL_ROOT = Path(__file__).resolve().parent.parent          # godel_rest/
# skills may sit beside godel_rest/ (godel/.claude/skills), inside it after a
# transfer, or in the live workspace layout (workspace/skills/) — support all.
# The workspace path is the one that actually exists on Hayden's machine; the
# other two are kept for transferred copies.
_CANDIDATES = [GODEL_ROOT.parent / "skills",
               GODEL_ROOT.parent / ".claude" / "skills",
               GODEL_ROOT / ".claude" / "skills"]
SKILLS_DIR = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

# Only these invocations are allowed. Note: `broker.cli order`, `--confirm` and
# setup_auth are absent by design — see the module docstring.
_ALLOW = [
    re.compile(r"^python3 -m analytics\.run (status|verify|channels|skill|signals|"
               r"buzz|clusters|influence|activity|price|fundamentals|ratios|dcf|"
               r"profile|chart|fa|thesis|trials|trial|watch)\b"),
    re.compile(r"^python3 -m broker\.cli (auth|balances|positions|portfolio|risk|"
               r"fit|quote|hours)\b"),
    # trade authorization: propose asks Hayden, confirm places what he approved
    re.compile(r"^python3 -m broker\.cli propose [A-Z.]{1,6} "
               r"(BUY|SELL|SELL_SHORT|BUY_TO_COVER) \d+"
               r"( --limit \d+(\.\d+)?)?( --note .*)?$"),
    re.compile(r"^python3 -m broker\.cli propose-cancel [A-Za-z0-9_-]+"
               r"( --note .*)?$"),
    re.compile(r"^python3 -m broker\.cli confirm t-[0-9a-f]{8}"
               r"( --yes| [A-Z0-9-]{4,16})$"),
    re.compile(r"^python3 -m broker\.cli (pending|revoke)\b"),
    re.compile(r"^(ls|cat|head|tail)\b"),
]
# `order` as a standalone word blocks `broker.cli order` while leaving
# propose-cancel/pending alone; --confirm blocks the manual flag but not the
# `confirm` subcommand.
_DENY = re.compile(r"(\border\b|--confirm|setup_auth|GODEL_ALLOW_TRADING|"
                   r"GODEL_TRADING_HALT|GODEL_PRIVATE_DIR|trade-auth|"
                   r"trade-tickets|trade-confirmations|trade-audit|"
                   r"\brm\b|[;&|`>]|\$\(|sudo)")

# ls/cat/head/tail must stay inside the project tree: the confirmation codes
# and audit trail live outside it.
_READ_CMD = re.compile(r"^(ls|cat|head|tail)\b(.*)$")


def _read_paths_are_local(cmd: str) -> bool:
    m = _READ_CMD.match(cmd)
    if not m:
        return True
    for tok in m.group(2).split():
        if tok.startswith("-"):
            continue
        if tok.startswith("~") or tok.startswith("/") or "$" in tok:
            return False
        try:
            resolved = (GODEL_ROOT / tok).resolve()
        except OSError:
            return False
        if not str(resolved).startswith(str(GODEL_ROOT)):
            return False
    return True


_CD_PREFIX = re.compile(r"^cd\s+godel_rest\s*(&&|;)\s*")


def run_command(command: str, timeout: int = 240) -> str:
    cmd = command.strip()
    # commands already run from godel_rest/; tolerate the documented `cd` prefix
    cmd = _CD_PREFIX.sub("", cmd).strip()
    if _DENY.search(cmd):
        return ("BLOCKED: command contains a forbidden token (manual trading path / "
                "chaining / redirect). To trade, use `broker.cli propose ...` and "
                "then `broker.cli confirm <ticket> <code>` with the code Hayden "
                "sends you.")
    if not _read_paths_are_local(cmd):
        return ("BLOCKED: read commands are confined to the godel_rest/ tree.")
    if not any(p.match(cmd) for p in _ALLOW):
        return ("BLOCKED: command not on the allowlist. Allowed: "
                "`python3 -m analytics.run ...`, read-only `python3 -m broker.cli "
                "{auth,balances,positions,portfolio,risk,fit,quote,hours}`, "
                "trade authorization `python3 -m broker.cli "
                "{propose,propose-cancel,confirm,pending,revoke} ...`, "
                "ls/cat/head/tail inside godel_rest/.")
    try:
        out = subprocess.run(cmd, shell=True, cwd=str(GODEL_ROOT), timeout=timeout,
                             capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    res = (out.stdout or "") + (("\n[stderr] " + out.stderr) if out.stderr else "")
    return res[:6000] if res.strip() else "(no output)"


def read_skill(name: str) -> str:
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.exists():
        avail = ", ".join(sorted(p.name for p in SKILLS_DIR.iterdir() if p.is_dir()))
        return f"No skill named {name!r}. Available: {avail}"
    return path.read_text()[:8000]


def read_file(path: str) -> str:
    p = (GODEL_ROOT / path).resolve()
    if not str(p).startswith(str(GODEL_ROOT)) or not p.exists():
        return f"Cannot read {path!r} (must be a file under godel_rest/)."
    if p.suffix not in (".md", ".txt", ".json", ".csv"):
        return f"Refusing to read binary/other file type {p.suffix}."
    return p.read_text()[:8000]


def skill_index() -> str:
    """name: description for every skill — what a generic framework injects."""
    lines = []
    for d in sorted(SKILLS_DIR.iterdir()):
        sk = d / "SKILL.md"
        if not sk.exists():
            continue
        text = sk.read_text()
        m = re.search(r"name:\s*(.+)", text)
        desc = re.search(r"description:\s*(.+)", text)
        if m and desc:
            lines.append(f"- {m.group(1).strip()}: {desc.group(1).strip()}")
    return "\n".join(lines)


TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "read_skill",
        "description": "Read the full instructions for a named skill.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a read-only research command (see allowlist).",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text/markdown/json file under godel_rest/ (e.g. a generated report).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "Finish with your final answer (rating, recommendation, risks, artifact path).",
        "parameters": {"type": "object", "properties": {
            "answer": {"type": "string"}}, "required": ["answer"]}}},
]

DISPATCH = {"read_skill": read_skill, "run_command": run_command, "read_file": read_file}
