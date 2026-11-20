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
NEVER = re.compile(r"[`\n\r]|\$\(|\$\{|\.\./")       # expanded or split by the shell even inside double quotes
OPS = ";&|<>()"


def shell_operator(cmd: str) -> str | None:
    m = NEVER.search(cmd)
    if m:
        return m.group(0)
    import shlex
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=OPS)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return "unbalanced quotes"
    for t in toks:
        if t and all(c in OPS for c in t):
            return t
    return None
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


def text_only(ws: Path) -> bool:
    try:
        return bool(json.loads((ws / ".tcadcal_workspace.json").read_text()).get("text_only_model"))
    except (OSError, ValueError):
        return False


def runtime_tool_result(rp: Path, ws: Path) -> bool:
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(ws))
    parts = rp.parts
    return "tool-results" in parts and any(parts[i] == ".claude" and parts[i + 1] == "projects" and parts[i + 2] == enc
                                           for i in range(len(parts) - 3))


def first_unquoted_operator(cmd: str) -> int | None:
    q = None
    for i, c in enumerate(cmd):
        if q:
            if c == q:
                q = None
            elif q == '"' and (c == "`" or cmd.startswith("$(", i)):
                return i
            continue
        if c in "'\"":
            q = c
        elif c in OPS or c in "`\n\r" or cmd.startswith("$(", i) or cmd.startswith("${", i):
            return i
    return None


def decide(ev: dict, ws: Path) -> tuple[str, str]:
    tool = ev.get("tool_name", "")
    inp = ev.get("tool_input", {}) or {}
    if tool in ALWAYS:
        return "allow", ""
    if tool in READ_TOOLS:
        p = inp.get(READ_TOOLS[tool]) or str(ws)
        rp = Path(p if os.path.isabs(p) else ws / p).resolve()
        if tool == "Read" and runtime_tool_result(rp, ws):
            return "allow", ""
        if rp != ws and ws not in rp.parents:
            return "deny", f"{tool} outside the workspace is not allowed: {p} (the workspace is {ws}; its paths start there)"
        if tool == "Glob" and ".." in str(inp.get("pattern", "")):
            return "deny", "Glob patterns may not climb out of the workspace"
        if tool == "Read" and rp.suffix.lower() in C.IMAGE_SUFFIXES and text_only(ws):
            return "deny", C.TEXT_ONLY_IMAGE_REASON
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
        if not BASH_RE.match(cmd) or shell_operator(cmd):
            i = first_unquoted_operator(cmd)
            head = re.sub(r"\s+\d+$", "", cmd[:i].strip()) if i is not None else cmd   # "2>&1": drop the fd number
            if i is not None and BASH_RE.match(head) and not shell_operator(head):
                return "deny", (f"Bash runs `python3 tools/<tool>.py <args>` only, without shell operators or "
                                f"redirection ({cmd[i]!r} here; the tools already print errors as JSON). "
                                f"Run exactly: {head} (when its output is too long to show, it is saved to a file "
                                "whose path the tool result names; read that file with Read, in parts)")
            return "deny", ("Bash is limited to `python3 tools/<tool>.py <args>` without shell operators (text with "
                            "'>' or '&' is fine inside quotes). Read files with Read, list them with Glob, write notes "
                            "and proposals with Write or Edit, and validate a proposal with "
                            "`python3 tools/state.py propose P###`")
        return "allow", ""
    if tool in C.WEB_SEARCH_TOOLS:
        hit = web_blocked(str(inp.get(C.WEB_SEARCH_TOOLS[tool], "")))
        if hit:
            return "deny", f"query blocked (targets this project's own publications: '{hit}'); use independent sources"
        return "allow", ""
    if tool in C.WEB_FETCH_TOOLS:
        urls = inp.get(C.WEB_FETCH_TOOLS[tool], "")
        urls = [urls] if isinstance(urls, str) else list(urls or [])
        if not urls:
            return "deny", "no URL given"
        for url in urls:
            url = str(url)
            if not re.match(r"^https?://", url) or re.search(r"(?i)localhost|127\.0\.0\.1|file:", url):
                return "deny", "only public http(s) URLs may be fetched"
            hit = web_blocked(url) or web_blocked(str(inp.get("prompt", "")))
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
