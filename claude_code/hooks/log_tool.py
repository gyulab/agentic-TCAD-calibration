"""PostToolUse logger: records every tool call with its input and (truncated) output in
<workspace>/logs/tools.jsonl. Web results are kept in full in logs/web.jsonl and screened
for the project's own publications (a hit is logged as a leak event and flagged to the agent)."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guard import web_blocked  # noqa: E402


def screened(text: str, ws: Path) -> str:
    text = re.sub(r"\[Binary content \([^)]*\) also saved to [^\]]*\]", "", text)
    for p in (str(ws), str(ws).replace("/", "-")):
        text = text.replace(p, "")
    return text


def main() -> int:
    ev = json.loads(sys.stdin.read() or "{}")
    ws = Path(os.environ.get("TCADCAL_WS") or ev.get("cwd") or ".").resolve()
    utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tool = ev.get("tool_name")
    resp = ev.get("tool_response")
    text = resp if isinstance(resp, str) else json.dumps(resp, default=str)
    rec = {"utc": utc, "tool": tool, "input": ev.get("tool_input"), "output": text[:20000],
           "output_len": len(text), "session": ev.get("session_id")}
    logs = ws / "logs"
    with open(logs / "tools.jsonl", "a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")
    if tool in ("WebSearch", "WebFetch"):
        with open(logs / "web.jsonl", "a") as fh:
            fh.write(json.dumps({**rec, "output": text}, default=str) + "\n")
        hit = web_blocked(screened(text, ws))
        if hit:
            with open(logs / "leak_events.jsonl", "a") as fh:
                fh.write(json.dumps({"utc": utc, "tool": tool, "input": ev.get("tool_input"), "term": hit}) + "\n")
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
                  "Harness notice: this result contains material from this calibration project's own publications "
                  f"(matched '{hit}'). Do not use that material; rely on independent sources."}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
