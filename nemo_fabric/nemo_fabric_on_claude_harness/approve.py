#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from tcadcal import config as C  # noqa: E402
from tcadcal.util import append_jsonl, now_utc, read_json, sha256_file, write_json  # noqa: E402


def gate_times(odir: Path, ws: Path, pid: str, decision_utc: str, review_min: float | None) -> dict:
    from datetime import datetime
    def t(s):
        return datetime.fromisoformat(s) if s else None
    gate = read_json(odir / "gate_pending.json") or {}
    req = gate.get("since_utc") if gate.get("proposal") == pid else None
    pres = (read_json(ws / "approvals" / f"{pid}.presented.json") or {}).get("presented_utc")
    dec = t(decision_utc)
    span = lambda a, b: None if a is None or b is None else round((b - a).total_seconds())  # noqa: E731
    return {"gate_requested_utc": req, "presented_utc": pres, "decision_utc": decision_utc,
            "presentation_lag_s": span(t(req), t(pres)), "user_response_s": span(t(pres), dec),
            "run_wait_s": span(t(req), dec), "user_review_min": review_min}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--approver", default="user")
    ap.add_argument("pid")
    ap.add_argument("decision", nargs="?", choices=["approve", "reject"])
    ap.add_argument("comment", nargs="?", default="")
    ap.add_argument("--reviewed-sha", help="sha256 (>= 12 hex characters) of the proposal version the reviewer saw")
    ap.add_argument("--review-min", type=float, help="actual review time in minutes, as stated by the reviewer")
    ap.add_argument("--rationale", default="",
                    help="the reviewer's rationale and evidence, for the record only (not sent to the agent)")
    ap.add_argument("--fix-to-reference", help="comma-separated parameters that this decision fixes at the reference card "
                    "values (bounds reviews only, e.g. WF_gate,WF_sd)")
    a = ap.parse_args()
    ws = C.RUNS_ROOT / a.run_id / "workspace"
    prop = ws / "proposals" / f"{a.pid}.json"
    if a.show:
        pres = ws / "approvals" / f"{a.pid}.presented.json"
        if not pres.exists() and prop.exists():
            write_json(pres, {"proposal": a.pid, "presented_utc": now_utc(), "proposal_sha256": sha256_file(prop)})
        md = ws / "proposals" / f"{a.pid}.md"
        print(md.read_text() if md.exists() else "")
        print(json.dumps(read_json(prop), indent=1))
        print(json.dumps(read_json(ws / "proposals" / f"{a.pid}.validated.json"), indent=1))
        print(f"proposal sha256 (for --reviewed-sha): {sha256_file(prop)}")
        return
    if not a.decision:
        raise SystemExit("decision required (approve | reject)")
    val = read_json(ws / "proposals" / f"{a.pid}.validated.json")
    if not val or not val.get("ok") or val.get("sha256") != sha256_file(prop):
        raise SystemExit(f"{a.pid} is not validated in its current form; nothing recorded")
    cur = sha256_file(prop)
    if not a.reviewed_sha or len(a.reviewed_sha) < 12 or not cur.startswith(a.reviewed_sha.lower()):
        raise SystemExit(f"{a.pid}: --reviewed-sha {a.reviewed_sha!r} does not match the current proposal ({cur[:16]}...). "
                         "The proposal changed after the review, or no reviewed version was given; nothing recorded. "
                         "Re-present the current version to the reviewer.")
    rec = {"proposal": a.pid, "decision": a.decision, "comment": a.comment, "approver": a.approver,
           "proposal_sha256": val["sha256"], "reviewed_sha256_prefix": a.reviewed_sha.lower(), "utc": now_utc()}
    rec.update({"review_type": "user decision", "rationale": a.rationale, "proposal_type": read_json(prop).get("type")})
    rec["time"] = gate_times(C.RUNS_ROOT / a.run_id / "orchestrator", ws, a.pid, rec["utc"], a.review_min)
    prev = read_json(ws / "approvals" / f"{a.pid}.json")
    if prev:
        if prev.get("proposal_sha256") == val["sha256"]:
            raise SystemExit(f"{a.pid} already has a decision")
        hist = ws / "approvals" / "history"
        hist.mkdir(exist_ok=True)
        k = 1
        while (hist / f"{a.pid}.decision{k}.json").exists():
            k += 1
        (ws / "approvals" / f"{a.pid}.json").rename(hist / f"{a.pid}.decision{k}.json")
        rec["supersedes"] = {"decision": prev.get("decision"), "proposal_sha256": prev.get("proposal_sha256"),
                             "utc": prev.get("utc"), "archived_as": f"approvals/history/{a.pid}.decision{k}.json"}
    if a.fix_to_reference:
        from tcadcal.state import Workspace, apply_reviewer_fix
        if read_json(prop).get("type") != "bounds":
            raise SystemExit("--fix-to-reference applies to bounds reviews only")
        rec["applied"] = apply_reviewer_fix(Workspace(ws), a.pid, [n for n in a.fix_to_reference.split(",") if n])
    write_json(ws / "approvals" / f"{a.pid}.json", rec)
    append_jsonl(C.RUNS_ROOT / a.run_id / "orchestrator" / "approvals.jsonl", rec)
    print(json.dumps(rec, indent=1))


if __name__ == "__main__":
    main()
