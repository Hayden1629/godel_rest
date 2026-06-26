"""Sandboxed tools the test agent may call. The command allowlist enforces the
no-trading guarantee at the harness level (defense in depth — orders are also
blocked in broker/orders.py)."""
import re
import subprocess
from pathlib import Path

GODEL_ROOT = Path(__file__).resolve().parent.parent          # godel_rest/
# skills may sit beside godel_rest/ (godel/.claude/skills) or inside it after a
# transfer — support both.
_CANDIDATES = [GODEL_ROOT.parent / ".claude" / "skills",
               GODEL_ROOT / ".claude" / "skills"]
SKILLS_DIR = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

# Only these invocations are allowed. Note: broker order/setup_auth are absent.
_ALLOW = [
    re.compile(r"^python3 -m analytics\.run (status|verify|channels|skill|signals|"
               r"buzz|clusters|influence|activity|price|fundamentals|ratios|dcf|"
               r"profile|chart|fa|thesis)\b"),
    re.compile(r"^python3 -m broker\.cli (auth|balances|positions|portfolio|risk|"
               r"fit|quote|hours)\b"),
    re.compile(r"^(ls|cat|head|tail)\b"),
]
_DENY = re.compile(r"(order|--confirm|setup_auth|GODEL_ALLOW_TRADING|\brm\b|"
                   r"[;&|`>]|\$\(|sudo)")


_CD_PREFIX = re.compile(r"^cd\s+godel_rest\s*(&&|;)\s*")


def run_command(command: str, timeout: int = 240) -> str:
    cmd = command.strip()
    # commands already run from godel_rest/; tolerate the documented `cd` prefix
    cmd = _CD_PREFIX.sub("", cmd).strip()
    if _DENY.search(cmd):
        return ("BLOCKED: command contains a forbidden token (trading / chaining / "
                "redirect). This agent is read-only and cannot trade.")
    if not any(p.match(cmd) for p in _ALLOW):
        return ("BLOCKED: command not on the allowlist. Allowed: "
                "`python3 -m analytics.run ...`, read-only `python3 -m broker.cli "
                "{auth,balances,positions,portfolio,risk,fit,quote,hours}`, "
                "ls/cat/head/tail.")
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
