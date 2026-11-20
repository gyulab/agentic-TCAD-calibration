"""InstructionsLoaded hook: records which instruction files (CLAUDE.md, rules) Claude Code
loaded into the agent's context, so isolation can be audited."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    raw = sys.stdin.read() or "{}"
    ev = json.loads(raw)
    ws = Path(os.environ.get("TCADCAL_WS") or ev.get("cwd") or ".").resolve()
    rec = {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **{k: v for k, v in ev.items()
                                                                              if k != "transcript_path"}}
    with open(ws / "logs" / "instructions_loaded.jsonl", "a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
