"""Workspace job waits, author-review waits and protocol helpers."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE / "lib"))

from tcadcal.util import append_jsonl, now_utc, read_json, sha256_file, write_json

PROTO = re.compile(r"^\s*(WAIT_JOB\s+(J\d{4})|APPROVAL_REQUIRED\s+(P\d{3})|FINAL_DONE)\s*$", re.M)

KICKOFF = ("Begin calibration run {run_id}. Follow AGENTS.md and the calibration-loop skill. "
           "End this turn with one protocol line.")

def pending_user_notes(odir: Path) -> list[dict]:
    from tcadcal.util import read_jsonl
    done = {d.get("id") for d in read_jsonl(odir / "user_notes_delivered.jsonl")}
    return [n for n in read_jsonl(odir / "user_notes.jsonl") if n.get("id") not in done]

def with_user_notes(odir: Path, message: str, turn: int) -> str:
    notes = pending_user_notes(odir)
    if not notes:
        return message
    text = "\n".join(f"[user note {n['id']}, {n['utc']}, facts only] {n['text']}" for n in notes)
    for n in notes:
        append_jsonl(odir / "user_notes_delivered.jsonl", {"id": n["id"], "utc": now_utc(), "turn": turn})
    return f"{text}\n{message}"

def wait_job(ws: Path, jid: str, poll: float) -> dict:
    sys.path.insert(0, str(HERE / "lib"))
    from tcadcal.jobs import status
    from tcadcal.state import Workspace
    w = Workspace(ws)
    while True:
        try:
            s = status(w, jid)
        except FileNotFoundError:
            return {"state": "unknown"}
        if s["state"] not in ("queued", "running"):
            return s
        time.sleep(poll)

def wait_approval(ws: Path, odir: Path, pid: str, poll: float) -> dict:
    val = read_json(ws / "proposals" / f"{pid}.validated.json")
    prop = ws / "proposals" / f"{pid}.json"
    if not val or not val.get("ok") or not prop.exists() or val.get("sha256") != sha256_file(prop):
        return {"decision": "not_submitted", "comment": f"{pid} is not validated in its current form. "
                "Run `python3 tools/state.py propose " + pid + "` and fix the errors first."}
    gate = {"proposal": pid, "since_utc": now_utc(), "type": val.get("type"), "workspace": str(ws)}
    write_json(odir / "gate_pending.json", gate)
    append_jsonl(odir / "gates.jsonl", {**gate, "event": "requested"})
    while True:
        ap = read_json(ws / "approvals" / f"{pid}.json")
        if ap and ap.get("proposal_sha256") == val["sha256"]:
            (odir / "gate_pending.json").unlink(missing_ok=True)
            append_jsonl(odir / "gates.jsonl", {"proposal": pid, "event": "answered", "utc": now_utc(),
                                                "decision": ap["decision"], "approver": ap.get("approver"),
                                                "time": ap.get("time")})
            return ap
        time.sleep(poll)
