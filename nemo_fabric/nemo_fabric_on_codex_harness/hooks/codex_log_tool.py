"""PostToolUse logger for the Codex harness: hooks/log_tool.py after the MCP tool name is written as
mcp__<server>__<tool>, so that web results reach logs/web.jsonl as in the Claude
Code harness."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_guard as CG  # noqa: E402
import log_tool as L  # noqa: E402


def main() -> int:
    ev = json.loads(sys.stdin.read() or "{}")
    nm = CG.mcp_name(str(ev.get("tool_name", "")))
    if nm:
        ev["tool_name"] = f"mcp__{nm[0]}__{nm[1]}"
    sys.stdin = io.StringIO(json.dumps(ev))
    return L.main()


if __name__ == "__main__":
    sys.exit(main())
