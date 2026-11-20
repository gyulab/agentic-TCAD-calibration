"""PreToolUse guard for the Codex harness: the decisions of hooks/guard.py, mapped to Codex tool calls.

The rule is the one of the Claude Code harness (guard.decide): reads only inside the workspace, writes only in
proposals/ notes/ kb/literature/, the agent tools only as `python3 tools/<tool>.py ...`, and web queries and pages
checked against the same block list. Codex has no Read, Glob, Grep, Write or Edit tools, so:

- a read-only shell command (cat, head, tail, sed -n, nl, wc, ls, grep, rg, find without actions) is decided as Read of
  each file argument (the script of sed and the pattern of grep and rg are not files);
- output-only shell forms are allowed when every part is allowed on its own: a tool run or a read-only command piped
  into head, tail, grep, rg, sed -n, wc, sort, uniq, cut, nl or cat, commands joined with ; && ||, echo and printf, and
  stderr merged (2>&1) or discarded (to /dev/null). Writing with the shell, a pipe into any other program, &, <,
  subshells, substitution and every read outside the workspace stay refused;
- the ws_files MCP tools write_file and edit_file (hooks/ws_files_mcp.py) are decided as Write and Edit;
- view_image is decided as Read of the image; update_plan is allowed like TodoWrite;
- the Exa MCP tools keep their names and inputs.

A refusal exits with code 2 and the reason on stderr, which Codex returns to the model. Every decision is appended to
<workspace>/logs/guard.jsonl with the fields of guard.py and "harness": "codex".
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard as G  # noqa: E402  (same decisions, same block list, same config)

READ_CMDS = {"cat", "head", "tail", "sed", "nl", "wc", "ls", "grep", "rg", "find", "stat", "file"}
FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
# output-only shell forms: a pipe may feed these filters, which only read their input
FILTERS = {"head", "tail", "grep", "rg", "sed", "wc", "sort", "uniq", "cut", "nl", "cat"}
TEXT_CMDS = {"echo", "printf", "true"}
SAFE_REDIRECT = re.compile(r"\s*(?:2>&1|2>\s*/dev/null|>\s*/dev/null|&>\s*/dev/null|1>\s*/dev/null)(?=\s|$|[;|&])")
OUTSIDE_HELP = (" Files outside this directory are not available in this session. The rules of the proposal check are "
                "in the skills (calibration-loop, deck-patching) and in the messages that "
                "`python3 tools/state.py propose P###` prints.")
SHELL_HELP = ("Shell commands are limited to `python3 tools/<tool>.py <args>` and the read-only commands cat, head, "
              "tail, sed -n, nl, wc, ls, grep, rg and find inside this directory. Their output may be piped into head, "
              "tail, grep, sed -n, wc, sort, uniq, cut or nl, commands may be joined with ; && ||, and 2>&1 is fine. "
              "Nothing may be written with the shell: write notes and proposals with the ws_files tools write_file and "
              "edit_file, and validate a proposal with `python3 tools/state.py propose P###`." + OUTSIDE_HELP)


def mcp_name(tool: str) -> tuple[str, str] | None:
    m = re.match(r"^mcp__([A-Za-z0-9_-]+?)__([A-Za-z0-9_-]+)$", tool) or re.match(r"^([A-Za-z0-9_-]+)[./:]([A-Za-z0-9_-]+)$", tool)
    return (m.group(1), m.group(2)) if m else None


PATTERN_OPTS = ("-e", "--regexp", "--expression")


def path_args(argv: list[str]) -> list[str]:
    """The arguments of a read-only command that name files. The script of sed and the pattern of grep and rg are not
    files; a file given with -f is."""
    toks = argv[1:]
    given = any(t in PATTERN_OPTS or t.startswith(("--regexp=", "--expression=")) or t in ("-f", "--file") for t in toks)
    pending = argv[0] in ("sed", "grep", "rg") and not given
    out, skip = [], False
    for t in toks:
        if skip:
            skip = False
            continue
        if t in PATTERN_OPTS:
            skip = True
            continue
        if t.startswith("-"):
            continue
        if pending:
            pending = False
            continue
        out.append(t)
    return out


def read_only(cmd: str, ws: Path) -> tuple[str, str] | None:
    """Decide a read-only shell command as Read of its path arguments. None if the command is not read-only."""
    if G.shell_operator(cmd):
        return None
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    if not argv or argv[0] not in READ_CMDS:
        return None
    if argv[0] == "sed":
        if "-n" not in argv and "--quiet" not in argv:
            return None
        if any(a.startswith(("-i", "--in-place")) for a in argv[1:]):
            return None
        script = next((a for a in argv[1:] if not a.startswith("-")), "")
        if re.search(r"(^|[;}\s])[ewWr]\b|\bw\s|/w\s|/e\b", script):
            return None
    if argv[0] == "find" and any(a in FIND_ACTIONS for a in argv[1:]):
        return None
    paths = path_args(argv)
    for p in paths or ["."]:
        d, r = G.decide({"tool_name": "Read", "tool_input": {"file_path": p}}, ws)
        if d != "allow":
            return d, r
    return "allow", "read-only command"


def split_unquoted(cmd: str) -> list[tuple[str, str]] | None:
    """Split a command on | ; && || outside quotes into (operator before, segment). None if any other shell operator
    (< > & ( )) stays outside quotes, a quote is open, or a segment is empty."""
    parts, cur, q, op, i = [], [], None, "", 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            cur.append(c)
            q = None if c == q else q
            i += 1
            continue
        if c in "'\"":
            q = c
            cur.append(c)
            i += 1
            continue
        if cmd[i:i + 2] in ("&&", "||"):
            parts.append((op, "".join(cur).strip()))
            cur, op, i = [], cmd[i:i + 2], i + 2
            continue
        if c in "|;":
            parts.append((op, "".join(cur).strip()))
            cur, op, i = [], c, i + 1
            continue
        if c in "<>&()":
            return None
        cur.append(c)
        i += 1
    if q:
        return None
    parts.append((op, "".join(cur).strip()))
    return None if any(not s for _, s in parts) else parts


def paths_inside(argv: list[str], ws: Path) -> tuple[str, str] | None:
    for p in path_args(argv):
        d, r = G.decide({"tool_name": "Read", "tool_input": {"file_path": p}}, ws)
        if d != "allow":
            return d, r
    return None


def segment(seg: str, after: str, ws: Path) -> tuple[str, str]:
    """Decide one segment of a command list or pipeline. After '|' only an input filter is allowed."""
    try:
        argv = shlex.split(seg)
    except ValueError:
        return "deny", "unbalanced quotes"
    if not argv:
        return "deny", "empty command"
    if after == "|":
        if argv[0] not in FILTERS:
            return "deny", f"`{argv[0]}` may not receive piped output; use head, tail, grep, sed -n, wc, sort, uniq, cut or nl"
        if argv[0] == "sed" and (read_only(seg, ws) is None):
            return "deny", "only `sed -n` without w or e commands may read piped output"
        return paths_inside(argv, ws) or ("allow", "")
    if argv[0] in TEXT_CMDS:
        return "allow", ""
    d, r = G.decide({"tool_name": "Bash", "tool_input": {"command": seg}}, ws)
    if d == "allow":
        return d, r
    ro = read_only(seg, ws)
    if ro is not None:
        return ro
    return "deny", f"`{argv[0]}` is not an allowed command"


def compound(cmd: str, ws: Path) -> tuple[str, str] | None:
    """A command list or pipeline whose parts are all allowed (tool runs, read-only commands, echo/printf,
    piped into input filters), with stderr merged or discarded. None if the form does not apply."""
    if G.NEVER.search(cmd):
        return None
    stripped = SAFE_REDIRECT.sub(" ", cmd).strip()
    parts = split_unquoted(stripped)
    if not parts:
        return None                      # another operator (a file redirection, &, <, a subshell) stays: refused
    if len(parts) == 1 and stripped == cmd:
        return None                      # a single plain command was already decided above
    for after, seg in parts:
        d, r = segment(seg, after, ws)
        if d != "allow":
            return "deny", r + (OUTSIDE_HELP if "outside the workspace" in r else "")
    return "allow", "output-only shell form"


def decide(ev: dict, ws: Path) -> tuple[str, str]:
    tool = ev.get("tool_name", "")
    inp = ev.get("tool_input", {}) or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {"command": inp}
    if tool == "Bash":
        if str(inp.get("sandbox_permissions", "")).startswith("require"):
            return "deny", "commands run inside the workspace sandbox only"
        wd = inp.get("workdir") or inp.get("cwd")
        if wd:
            rp = Path(wd if os.path.isabs(wd) else ws / wd).resolve()
            if rp != ws and ws not in rp.parents:
                return "deny", f"the working directory must stay in the workspace ({ws})"
        cmd = (inp.get("command") or "")
        cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        d, r = G.decide({"tool_name": "Bash", "tool_input": {"command": cmd}}, ws)
        if d == "allow":
            return d, r
        ro = read_only(cmd.strip(), ws)
        if ro is not None:
            return (ro[0], ro[1] + OUTSIDE_HELP) if ro[0] == "deny" and "outside the workspace" in ro[1] else ro
        cp = compound(cmd.strip(), ws)
        if cp is not None:
            return cp
        return "deny", SHELL_HELP
    if tool in ("update_plan", "TodoWrite"):
        return "allow", ""
    if tool == "view_image":
        return G.decide({"tool_name": "Read", "tool_input": {"file_path": inp.get("path", "")}}, ws)
    nm = mcp_name(tool)
    if nm and nm[0] == "ws_files" and nm[1] in ("write_file", "edit_file"):
        return G.decide({"tool_name": "Write" if nm[1] == "write_file" else "Edit",
                         "tool_input": {"file_path": inp.get("path", "")}}, ws)
    if nm and nm[0] == "exa":
        return G.decide({"tool_name": f"mcp__exa__{nm[1]}", "tool_input": inp}, ws)
    return "deny", f"tool {tool} is not available in this workspace"


def main() -> int:
    ev = json.loads(sys.stdin.read() or "{}")
    ws = Path(os.environ.get("TCADCAL_WS") or ev.get("cwd") or ".").resolve()
    decision, reason = decide(ev, ws)
    rec = {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool": ev.get("tool_name"),
           "input": ev.get("tool_input"), "decision": decision, "reason": reason, "session": ev.get("session_id"),
           "harness": "codex"}
    try:
        with open(ws / "logs" / "guard.jsonl", "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass
    if decision == "deny":
        print(reason or "refused by the harness guard", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
