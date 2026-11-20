"""PreToolUse guard for the calibration agent (Claude Code hook).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from tcadcal import config as C  # noqa: E402

TOOLS = C.AGENT_TOOLS
BASH_RE = re.compile(r"^python3 tools/(" + "|".join(TOOLS) + r")\.py(\s.*)?$", re.S)
META = re.compile(r"[;&|<>`\n\r]|\$\(|\$\{|\.\./")
WRITABLE = ("proposals", "notes", "kb/literature")
READ_TOOLS = {"Read": "file_path", "Glob": "path", "Grep": "path"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path"}
ALWAYS = {"Skill", "TodoWrite"}


def web_blocked(text: str) -> str | None:
    t = text.lower()
    for term in C.WEB_BLOCK_TERMS:
        if term in t:
            return term
    words = set(re.findall(r"[a-z0-9]+", t))
    for a, b in C.WEB_BLOCK_COMBOS:
        if a in words and b in words:
            return f"{a}+{b}"
    return None


def decide(ev: dict, ws: Path) -> tuple[str, str]:
    tool = ev.get("tool_name", "")
    inp = ev.get("tool_input", {}) or {}
    if tool in ALWAYS:
        return "allow", ""
    if tool in READ_TOOLS:
        p = inp.get(READ_TOOLS[tool]) or str(ws)
        rp = Path(p if os.path.isabs(p) else ws / p).resolve()
        if rp != ws and ws not in rp.parents:
            return "deny", f"{tool} outside the workspace is not allowed: {p}"
        if tool == "Glob" and ".." in str(inp.get("pattern", "")):
            return "deny", "Glob patterns may not climb out of the workspace"
        return "allow", ""
    if tool in WRITE_TOOLS:
        p = inp.get(WRITE_TOOLS[tool], "")
        rp = Path(p if os.path.isabs(p) else ws / p).resolve()
        if not any((ws / d) in rp.parents for d in WRITABLE):
            return "deny", f"writing is allowed only in {', '.join(WRITABLE)}/ (got {p})"
        if (ws / "proposals" / "history") in rp.parents or rp.name.endswith((".validated.json", ".validation_history.jsonl")):
            return "deny", f"validation records are written by the tools only (got {p})"
        return "allow", ""
    if tool == "Bash":
        cmd = (inp.get("command") or "").strip()
        if inp.get("run_in_background"):
            return "deny", "background shell commands are not allowed; tools submit their own jobs"
        if not BASH_RE.match(cmd) or META.search(cmd):
            return "deny", "Bash is limited to `python3 tools/<tool>.py <args>` without shell operators"
        return "allow", ""
    if tool == "WebSearch":
        hit = web_blocked(inp.get("query", ""))
        if hit:
            return "deny", f"query blocked (targets this project's own publications: '{hit}'); use independent sources"
        return "allow", ""
    if tool == "WebFetch":
        url = inp.get("url", "")
        if not re.match(r"^https?://", url) or re.search(r"(?i)localhost|127\.0\.0\.1|file:", url):
            return "deny", "only public http(s) URLs may be fetched"
        hit = web_blocked(url) or web_blocked(inp.get("prompt", ""))
        if hit:
            return "deny", f"URL blocked (this project's own publications: '{hit}')"
        return "allow", ""
    return "deny", f"tool {tool} is not available in this workspace"


def main() -> int:
    ev = json.loads(sys.stdin.read() or "{}")
    ws = Path(os.environ.get("TCADCAL_WS") or ev.get("cwd") or ".").resolve()
    decision, reason = decide(ev, ws)
    rec = {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool": ev.get("tool_name"),
           "input": ev.get("tool_input"), "decision": decision, "reason": reason, "session": ev.get("session_id")}
    try:
        with open(ws / "logs" / "guard.jsonl", "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass
    if decision == "deny":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                 "permissionDecisionReason": "harness guard"}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
