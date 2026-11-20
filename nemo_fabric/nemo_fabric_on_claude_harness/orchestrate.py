#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from tcadcal import config as C  # noqa: E402
from tcadcal.util import append_jsonl, now_utc, read_json, sha256_file, write_json  # noqa: E402

CLAUDE = os.environ.get("CLAUDE_BIN") or __import__("shutil").which("claude") or "claude"
TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Skill,TodoWrite"
PROTO = re.compile(r"^\s*(WAIT_JOB\s+(J\d{4})|APPROVAL_REQUIRED\s+(P\d{3})|FINAL_DONE)\s*$", re.M)
KICKOFF = ("Begin calibration run {run_id}. Follow CLAUDE.md and the calibration-loop skill. "
           "End this turn with one protocol line.")


def clean_env(ws: Path) -> dict[str, str]:
    keep = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "SHELL", "TZ", "TMPDIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PATH"] = f"{HERE / 'env' / 'bin'}:/usr/local/bin:/usr/bin:/bin"
    env.update({"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "TCADCAL_WS": str(ws), "BASH_DEFAULT_TIMEOUT_MS": "300000",
                "BASH_MAX_TIMEOUT_MS": "600000", "DISABLE_AUTOUPDATER": "1", "TMPDIR": str(C.ROOT / "tmp")})
    return env


def run_turn(ws: Path, odir: Path, st: dict, message: str, a: argparse.Namespace) -> dict:
    n = st["turn"] + 1
    argv = [CLAUDE, "-p", message, "--model", a.model, "--effort", a.effort, "--setting-sources", "project",
            "--strict-mcp-config", "--output-format", "stream-json", "--verbose", "--tools", TOOLS,
            "--permission-mode", "dontAsk", "--permission-prompts", "none", "--include-hook-events"]
    argv += ["--resume", st["session_id"]] if st.get("started") else ["--session-id", st["session_id"]]
    out_path = odir / "turns" / f"turn_{n:04d}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    k = 1
    while out_path.exists():
        out_path = odir / "turns" / f"turn_{n:04d}_r{k}.jsonl"
        k += 1
    t0 = time.time()
    with open(out_path, "w") as out, open(out_path.with_suffix(".stderr"), "w") as err:
        try:
            p = subprocess.run(argv, cwd=str(ws), env=clean_env(ws), stdout=out, stderr=err, timeout=a.turn_timeout,
                               stdin=subprocess.DEVNULL)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    st["started"] = True
    st["turn"] = n
    result, last_text, init = None, "", None
    for line in out_path.read_text(errors="ignore").splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("type") == "system" and m.get("subtype") == "init":
            init = m
        if m.get("type") == "assistant":
            for c in m.get("message", {}).get("content", []):
                if c.get("type") == "text":
                    last_text = c["text"]
        if m.get("type") == "result":
            result = m
    final_text = (result or {}).get("result") or last_text
    quota = bool(result and result.get("is_error") and (result.get("api_error_status") == 429 or
                 re.search(r"(?i)(usage|hour|weekly) limit|resets \d", str(result.get("result")))))
    proto = PROTO.findall(final_text or "")
    rec = {"turn": n, "utc": now_utc(), "message": message, "rc": rc, "seconds": round(time.time() - t0, 1),
           "protocol": proto[-1][0] if proto else None, "result_subtype": (result or {}).get("subtype"),
           "num_turns": (result or {}).get("num_turns"), "usage": (result or {}).get("usage"),
           "total_cost_usd": (result or {}).get("total_cost_usd"), "is_error": (result or {}).get("is_error"),
           "quota_limited": quota, "result_text": (str((result or {}).get("result"))[:300] if quota else None),
           "transcript": out_path.name}
    if init:
        rec["init"] = {k: init.get(k) for k in ("model", "tools", "mcp_servers", "skills", "plugins", "slash_commands",
                                                 "permissionMode", "cwd", "apiKeySource", "claude_code_version")}
    append_jsonl(odir / "turns.jsonl", rec)
    return rec


def quota_wait_seconds(text: str | None, now: float | None = None) -> float:
    """Seconds until the usage limit resets ('... resets 2:20am (<time zone>)'), plus 3 min; 15 min if unknown."""
    import datetime as dt
    m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", text or "", re.I)
    if not m:
        return 900.0
    h, mi, ap = int(m.group(1)) % 12, int(m.group(2) or 0), m.group(3).lower()
    h += 12 if ap == "pm" else 0
    try:
        from zoneinfo import ZoneInfo
        z = re.search(r"\(([A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+)\)", text or "")
        tz = ZoneInfo(z.group(1)) if z else None
    except Exception:  # noqa: BLE001
        tz = None
    cur = dt.datetime.fromtimestamp(now or time.time(), tz)
    tgt = cur.replace(hour=h, minute=mi, second=0, microsecond=0)
    if tgt <= cur:
        if (cur - tgt).total_seconds() < 2 * 3600:   # the reset time has just passed
            return 180.0
        tgt += dt.timedelta(days=1)
    return min(max((tgt - cur).total_seconds() + 180.0, 60.0), 6 * 3600.0)


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="xhigh")
    ap.add_argument("--max-turns", type=int, default=250)
    ap.add_argument("--max-hours", type=float, default=48.0)
    ap.add_argument("--turn-timeout", type=int, default=7200)
    ap.add_argument("--poll", type=float, default=20.0)
    ap.add_argument("--message", help="first message (default: kickoff)")
    ap.add_argument("--stop-after-turns", type=int, help="stop after this many turns (preflight)")
    a = ap.parse_args()
    root = C.RUNS_ROOT / a.run_id
    ws, odir = root / "workspace", root / "orchestrator"
    st_path = odir / "state.json"
    st = read_json(st_path) or {"run_id": a.run_id, "session_id": str(uuid.uuid4()), "turn": 0, "started": False,
                                "t0": time.time(), "status": "running", "model": a.model, "effort": a.effort}
    write_json(st_path, st)
    message = a.message or (st.get("next_message") or KICKOFF.format(run_id=a.run_id))
    nudges = 0
    while True:
        if st["turn"] >= a.max_turns or time.time() - st["t0"] > a.max_hours * 3600:
            st["status"] = "stopped_cap"
            break
        message = with_user_notes(odir, message, st["turn"] + 1)
        rec = run_turn(ws, odir, st, message, a)
        write_json(st_path, st)
        if rec.get("quota_limited"):
            wait = quota_wait_seconds(rec.get("result_text"))
            brk = {"start_utc": now_utc(), "turn": rec["turn"], "reason": rec.get("result_text"), "wait_s": round(wait)}
            st["status"] = "quota_wait"
            write_json(st_path, st)
            time.sleep(wait)
            brk["end_utc"] = now_utc()
            append_jsonl(odir / "breaks.jsonl", brk)
            st["status"] = "running"
            write_json(st_path, st)
            continue
        proto = rec["protocol"] or ""
        if a.stop_after_turns and st["turn"] >= a.stop_after_turns:
            st["status"] = "stopped_preflight"
            st["next_message"] = None
            break
        if proto.startswith("WAIT_JOB"):
            nudges = 0
            jid = proto.split()[1]
            js = wait_job(ws, jid, a.poll)
            message = (f"[orchestrator] Job {jid} ({js.get('kind', '?')}) finished with state={js.get('state')}. "
                       f"Read it with: python3 tools/jobs.py show {jid}")
        elif proto.startswith("APPROVAL_REQUIRED"):
            nudges = 0
            pid = proto.split()[1]
            apr = wait_approval(ws, odir, pid, a.poll)
            if apr["decision"] == "not_submitted":
                message = f"[orchestrator] {apr['comment']}"
            else:
                message = (f"[orchestrator] Reviewer decision on {pid}: {apr['decision'].upper()}. "
                           f"Reviewer comment (verbatim): {apr.get('comment', '')!r}")
                if (apr.get("applied") or {}).get("note"):
                    message += f" Applied by the tools with this decision: {apr['applied']['note']}"
        elif proto == "FINAL_DONE":
            if (ws / "state" / "FROZEN").exists():
                st["status"] = "final_done"
                break
            message = "[orchestrator] FINAL_DONE requires an approved final proposal and `state freeze`. Continue."
        else:
            nudges += 1
            if nudges > 3:
                st["status"] = "stopped_no_protocol"
                break
            message = ("[orchestrator] Your last turn ended without a protocol line. Continue the calibration loop "
                       "and end the turn with WAIT_JOB <id>, APPROVAL_REQUIRED <id> or FINAL_DONE.")
        st["next_message"] = message
        write_json(st_path, st)
    st["finished_utc"] = now_utc()
    write_json(st_path, st)
    print(json.dumps(st, indent=1))


if __name__ == "__main__":
    main()
