"""File-writing tools for the Codex harness (MCP server over stdio).

The Claude Code harness gives the agent the Write and Edit tools, and the guard allows them only in proposals/, notes/
and kb/literature/. The Codex harness has no such tools behind the local gateway, so this server provides the same two
operations with the same rule: write_file (create or replace a file) and edit_file (replace one exact text). The
PreToolUse guard checks every call as it checks Write and Edit (hooks/codex_guard.py), and this server checks again.

    env/bin/python hooks/ws_files_mcp.py <workspace>
"""
from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

WS = Path(sys.argv[1]).resolve()
WRITABLE = ("proposals", "notes", "kb/literature")
app = MCPServer("ws_files", instructions="Write and edit files in proposals/, notes/ and kb/literature/ of this workspace.")


def _target(path: str) -> Path:
    p = Path(path)
    rp = (p if p.is_absolute() else WS / p).resolve()
    if not any((WS / d) in rp.parents for d in WRITABLE):
        raise ValueError(f"writing is allowed only in {', '.join(WRITABLE)}/ (got {path})")
    if (WS / "proposals" / "history") in rp.parents or rp.name.endswith((".validated.json", ".validation_history.jsonl")):
        raise ValueError(f"validation records are written by the tools only (got {path})")
    return rp


@app.tool(description="Create or replace a text file. path is relative to the workspace and must lie in proposals/, "
                      "notes/ or kb/literature/.")
def write_file(path: str, content: str) -> str:
    rp = _target(path)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(content)
    return f"wrote {rp.relative_to(WS)} ({len(content)} characters)"


@app.tool(description="Replace old_string with new_string in an existing text file. old_string must occur exactly once "
                      "unless replace_all is true. path follows the rule of write_file.")
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    rp = _target(path)
    if not rp.is_file():
        raise ValueError(f"{path} does not exist; create it with write_file")
    text = rp.read_text()
    n = text.count(old_string)
    if n == 0:
        raise ValueError("old_string was not found")
    if n > 1 and not replace_all:
        raise ValueError(f"old_string occurs {n} times; give more context or set replace_all")
    rp.write_text(text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1))
    return f"edited {rp.relative_to(WS)} ({n if replace_all else 1} replacement(s))"


if __name__ == "__main__":
    app.run("stdio")
