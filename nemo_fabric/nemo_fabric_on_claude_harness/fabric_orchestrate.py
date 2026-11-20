#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
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
sys.path.insert(0, str(HERE))
import fabric_backend as FB  # noqa: E402
import orchestrate as O  # noqa: E402
from tcadcal import config as C  # noqa: E402
from tcadcal.util import append_jsonl, now_utc, read_json, write_json  # noqa: E402

API_RETRY_WAITS = (60, 120, 300, 600, 900)       # then 900 s each
API_MAX_OUTAGE_H = 12.0
TRANSIENT = re.compile(r"(?i)\b(401|403|429|500|502|503|504|529)\b|rate.?limit|overloaded|timeout|timed out|temporar|"
                       r"connection (error|reset|refused)|api_error|service unavailable|unauthori[sz]ed|forbidden")
RUNTIME_STAGE = re.compile(r"""['"]stage['"]:\s*['"]invoke['"]|claude_runtime_unavailable|Claude invocation timed out""")
KEYWORD_LINE = re.compile(r"^\s*(WAIT_JOB|APPROVAL_REQUIRED|FINAL_DONE)\b[^\n]*$", re.M)
PROBE_TOOLS = [{"name": "kb_search", "description": "Search the Sentaurus Device manual and return matching chunks.",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]
PROBE_PROMPT = ("Find the Sentaurus manual section on the constant mobility model with the tool, then name "
                "the section in one sentence.")
RESTART_NOTE = ("The previous agent session of this run ended at turn {turn} because the agent runtime was lost "
                "({reason}). This is a new session in the same workspace; nothing in the workspace was changed. Read "
                "notes/journal.md, notes/steps/ and the output of `python3 tools/state.py show` before continuing.")


def suspect_turn(rec: dict, final_text: str) -> bool:
    """Endpoint-fault trigger: no protocol line, one inner turn (no tool use), under 50 output tokens, no keyword."""
    out = ((rec.get("usage") or {}).get("output_tokens") or 0)
    return (rec.get("rc") == "ok" and not rec.get("protocol") and (rec.get("num_turns") or 0) <= 1 and out < 50
            and not KEYWORD_LINE.search(final_text or ""))


def endpoint_probe(gateway: str, model: str, timeout: float = 120.0) -> dict:
    """One gateway check request (non-streaming). Healthy = a tool_use block in the answer."""
    import httpx
    t0 = time.time()
    body = {"model": model, "max_tokens": 1024, "tools": PROBE_TOOLS, "system": "You calibrate TCAD models.",
            "messages": [{"role": "user", "content": PROBE_PROMPT}]}
    h = {"x-api-key": os.environ.get("FABRIC_GATEWAY_KEY", ""), "anthropic-version": "2023-06-01",
         "content-type": "application/json"}
    try:
        r = httpx.post(gateway.rstrip("/") + "/v1/messages", json=body, headers=h, timeout=timeout)
        d = r.json() if r.status_code == 200 else {}
        types = [b.get("type") for b in d.get("content", [])]
        return {"healthy": "tool_use" in types, "status": r.status_code, "block_types": types,
                "seconds": round(time.time() - t0, 1)}
    except Exception as e:  # noqa: BLE001
        return {"healthy": False, "error": f"{type(e).__name__}: {e}"[:300], "seconds": round(time.time() - t0, 1)}


def endpoint_healthy(gateway: str, model: str, odir: Path, tries: int = 2) -> bool:
    for _ in range(tries):
        p = endpoint_probe(gateway, model)
        append_jsonl(odir / "endpoint_probes.jsonl", {"utc": now_utc(), **p})
        if p["healthy"]:
            return True
    return False


def runtime_failure(r: dict) -> str | None:
    """A failed Fabric answer that failed at the invoke stage or ran no model call means the runtime is gone."""
    err = json.dumps(r.get("error"), default=str)
    if RUNTIME_STAGE.search(err):
        m = re.search(r"""['"]code['"]:\s*['"]([a-z_]+)['"]""", err)
        return f"invoke {m.group(1) if m else 'failure'}"
    if not r.get("events") and not r.get("response") and not r.get("num_turns"):
        return "failed invocation with no events"
    return None


def wait_job_harness_env(ws: Path, jid: str, poll: float) -> dict:
    """orchestrate.wait_job, run with the harness's own Python (the job code needs the harness env)."""
    code = ("import json, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import orchestrate as O; "
            "print(json.dumps(O.wait_job(Path(sys.argv[2]), sys.argv[3], float(sys.argv[4]))))")
    p = subprocess.run([str(HERE / "env" / "bin" / "python"), "-c", code, str(HERE), str(ws), jid, str(poll)],
                       capture_output=True, text=True)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"state": "unknown", "error": (p.stderr or "")[-500:]}


def gateway_alive(url: str) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health/liveliness", timeout=10) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def ensure_gateway(url: str, odir: Path) -> str:
    if gateway_alive(url):
        return "alive"
    script = HERE / "fabric" / "gateway.sh"
    port = url.rstrip("/").rsplit(":", 1)[-1]
    if not script.exists():
        return "down, no launcher"
    subprocess.Popen(["setsid", str(script), port, str(HERE / "tmp" / f"gateway_{port}.log")],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(3)
        if gateway_alive(url):
            append_jsonl(odir / "breaks.jsonl", {"utc": now_utc(), "kind": "gateway_restart"})
            return "restarted"
    return "down, restart failed"


def init_info(events: list) -> dict | None:
    for e in events:
        m = e.get("message") if isinstance(e, dict) else None
        if isinstance(m, dict) and m.get("subtype") == "init":
            d = m.get("data") or m
            return {k: d.get(k) for k in ("model", "tools", "mcp_servers", "skills", "plugins", "permissionMode", "cwd",
                                          "apiKeySource", "claude_code_version") if k in d}
    return None


def transient_error(r: dict) -> bool:
    if not (r.get("failed") or r.get("status") not in ("succeeded", "RunStatus.SUCCEEDED")):
        return False
    return bool(TRANSIENT.search(json.dumps({k: r.get(k) for k in ("error", "subtype", "response")}, default=str)))


class Session:
    """One Fabric runtime (one agent session)."""

    def __init__(self, cfg, odir: Path):
        self.cfg, self.odir, self.rt, self.cm = cfg, odir, None, None

    async def start(self) -> None:
        from nemo_fabric import Fabric
        self.cm = await Fabric().start_runtime(self.cfg, base_dir=str(self.odir))
        self.rt = await self.cm.__aenter__()

    async def stop(self) -> None:
        if self.cm is not None:
            try:
                await asyncio.wait_for(self.cm.__aexit__(None, None, None), timeout=180)
            except Exception:  # noqa: BLE001  (includes the time limit: the next session starts regardless)
                pass
        self.cm = self.rt = None


async def run_turn(sess: Session, odir: Path, st: dict, message: str) -> dict:
    n = st["turn"] + 1
    path = odir / "turns" / f"turn_{n:04d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    k = 1
    while path.exists():   # transcripts are append-only, never overwritten (as orchestrate.run_turn)
        path = odir / "turns" / f"turn_{n:04d}_r{k}.json"
        k += 1
    t0 = time.time()
    lost = None
    try:
        res = await sess.rt.invoke(input=message)
        r = FB.result_record(res)
        failed = bool(r.get("failed")) or "fail" in str(r.get("status") or "").lower()
        rc = "error" if failed else "ok"
        lost = runtime_failure(r) if failed else None
    except Exception as e:  # noqa: BLE001  runtime loss (crashed host, stopped runtime, protocol timeout)
        r, rc, lost = {"error": f"{type(e).__name__}: {e}"[:2000], "events": []}, "exception", f"{type(e).__name__}"
    st["started"] = True
    st["turn"] = n
    events = r.pop("events", [])
    path.write_text(json.dumps({"message": message, "result": r, "events": events}, indent=1, default=str))
    final_text = r.get("response") or ""
    proto = O.PROTO.findall(final_text)
    rec = {"turn": n, "utc": now_utc(), "message": message, "rc": rc, "seconds": round(time.time() - t0, 1),
           "protocol": proto[-1][0] if proto else None, "result_subtype": r.get("subtype"),
           "num_turns": r.get("num_turns"), "usage": r.get("usage"), "total_cost_usd": None,
           "client_cost_estimate_usd": r.get("cost_usd"), "is_error": bool(r.get("failed")) or rc != "ok",
           "quota_limited": False,
           "api_transient": not lost and rc != "exception" and transient_error({**r, "status": r.get("status")}),
           "runtime_lost": lost, "error": (str(r.get("error"))[:600] if r.get("error") else None),
           "backend": "fabric", "model": st.get("model"), "runtime_id": r.get("runtime_id"),
           "session_id": r.get("session_id"), "invocation_id": r.get("invocation_id"), "transcript": path.name}
    if not proto:
        rec["endpoint_suspect"] = suspect_turn(rec, final_text)
    info = init_info(events)
    if info:
        rec["init"] = info
    if r.get("session_id"):
        st["session_id"] = r["session_id"]
    append_jsonl(odir / "turns.jsonl", rec)
    return rec


async def main_async(a: argparse.Namespace) -> dict:
    root = C.RUNS_ROOT / a.run_id
    ws, odir = root / "workspace", root / "orchestrator"
    os.environ["ADAPTER_PYTHON"] = sys.executable
    keys = FB.load_secrets()
    if not keys.get("FABRIC_GATEWAY_KEY"):
        raise SystemExit("gateway key file missing ($TCADCAL_SECRETS_DIR/litellm_master_key)")
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
    if str(FB.FABRIC_ENV_BIN) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{FB.FABRIC_ENV_BIN}:{os.environ.get('PATH', '')}"
    st_path = odir / "state.json"
    st = read_json(st_path) or {"run_id": a.run_id, "session_id": None, "turn": 0, "started": False, "t0": time.time(),
                                "status": "running", "model": a.model, "backend": "fabric", "restarts": 0,
                                "gateway": a.gateway, "context_tokens": a.context_tokens}
    cfg = FB.build_config(ws, odir, run_id=a.run_id, model=a.model, base_url=a.gateway, api_key_env="FABRIC_GATEWAY_KEY",
                          context_tokens=a.context_tokens, turn_timeout=a.turn_timeout)
    (odir / "fabric_config.json").write_text(cfg.model_dump_json(indent=1))
    write_json(st_path, st)
    message = a.message or (st.get("next_message") or O.KICKOFF.format(run_id=a.run_id))
    if st.get("started"):
        message = f"[user note, {now_utc()}, facts only] " + RESTART_NOTE.format(
            turn=st["turn"], reason="orchestrator process restarted") + "\n" + message
        st["restarts"] = st.get("restarts", 0) + 1
        append_jsonl(odir / "restarts.jsonl", {"utc": now_utc(), "turn": st["turn"], "reason": "orchestrator restart"})
    sess = Session(cfg, odir)
    await sess.start()
    nudges, outage_t0, api_try = 0, None, 0
    try:
        while True:
            if st["turn"] >= a.max_turns or time.time() - st["t0"] - st.get("break_s", 0.0) > a.max_hours * 3600:
                st["status"] = "stopped_cap"
                break
            message = O.with_user_notes(odir, message, st["turn"] + 1)
            rec = await run_turn(sess, odir, st, message)
            write_json(st_path, st)
            if rec["runtime_lost"]:
                append_jsonl(odir / "restarts.jsonl", {"utc": now_utc(), "turn": rec["turn"], "reason": rec["error"],
                                                       "kind": rec["runtime_lost"]})
                await sess.stop()
                if st.get("restarts", 0) >= a.max_restarts:
                    st["status"] = "stopped_runtime_lost"
                    break
                st["restarts"] = st.get("restarts", 0) + 1
                message = (f"[user note, {now_utc()}, facts only] "
                           + RESTART_NOTE.format(turn=rec["turn"], reason=rec["runtime_lost"]) + "\n" + message)
                write_json(st_path, st)
                await sess.start()
                continue
            if rec["api_transient"]:
                outage_t0 = outage_t0 or time.time()
                if time.time() - outage_t0 > a.max_outage_hours * 3600:
                    st["status"] = "stopped_api_unavailable"
                    break
                wait = API_RETRY_WAITS[min(api_try, len(API_RETRY_WAITS) - 1)]
                api_try += 1
                brk = {"start_utc": now_utc(), "turn": rec["turn"], "reason": rec["error"] or rec["result_subtype"],
                       "wait_s": wait, "kind": "api_transient"}
                st["status"] = "api_wait"
                write_json(st_path, st)
                tb = time.time()
                await asyncio.sleep(wait)
                if a.gateway.startswith("http://127.0.0.1"):
                    brk["gateway"] = await asyncio.to_thread(ensure_gateway, a.gateway, odir)
                # the turn's own seconds count as break time only when the agent did no work in it
                worked = (rec.get("num_turns") or 0) > 1
                st["break_s"] = st.get("break_s", 0.0) + (time.time() - tb) + (0.0 if worked else rec["seconds"])
                brk["end_utc"] = now_utc()
                append_jsonl(odir / "breaks.jsonl", brk)
                st["status"] = "running"
                write_json(st_path, st)
                continue
            if rec.get("endpoint_suspect") and not await asyncio.to_thread(endpoint_healthy, a.gateway, st["model"],
                                                                             odir):
                outage_t0 = outage_t0 or time.time()
                if time.time() - outage_t0 > a.max_outage_hours * 3600:
                    st["status"] = "stopped_api_unavailable"
                    break
                wait = API_RETRY_WAITS[min(api_try, len(API_RETRY_WAITS) - 1)]
                api_try += 1
                brk = {"start_utc": now_utc(), "turn": rec["turn"], "reason": "endpoint probe failed after a turn "
                       "with no tool use, no protocol keyword and under 50 output tokens", "wait_s": wait,
                       "kind": "endpoint_fault"}
                st["status"] = "api_wait"
                write_json(st_path, st)
                tb = time.time()
                await asyncio.sleep(wait)
                if a.gateway.startswith("http://127.0.0.1"):
                    brk["gateway"] = await asyncio.to_thread(ensure_gateway, a.gateway, odir)
                st["break_s"] = st.get("break_s", 0.0) + (time.time() - tb) + rec["seconds"]
                brk["end_utc"] = now_utc()
                append_jsonl(odir / "breaks.jsonl", brk)
                st["status"] = "running"
                write_json(st_path, st)
                continue
            outage_t0, api_try = None, 0
            proto = rec["protocol"] or ""
            if a.stop_after_turns and st["turn"] >= a.stop_after_turns:
                st["status"] = "stopped_preflight"
                st["next_message"] = None
                break
            if proto.startswith("WAIT_JOB"):
                nudges = 0
                jid = proto.split()[1]
                js = await asyncio.to_thread(wait_job_harness_env, ws, jid, a.poll)
                message = (f"[orchestrator] Job {jid} ({js.get('kind', '?')}) finished with state={js.get('state')}. "
                           f"Read it with: python3 tools/jobs.py show {jid}")
            elif proto.startswith("APPROVAL_REQUIRED"):
                nudges = 0
                pid = proto.split()[1]
                apr = await asyncio.to_thread(O.wait_approval, ws, odir, pid, a.poll)
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
    finally:
        await sess.stop()
        st["finished_utc"] = now_utc()
        write_json(st_path, st)
    return st


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--model", default="nemotron-3.5-super-vl", help="gateway model name (fabric/litellm.yaml)")
    ap.add_argument("--gateway", default="http://127.0.0.1:4000")
    ap.add_argument("--context-tokens", type=int, default=500000,
                    help="declared context window (500k tokens in the calibrations of the manuscript)")
    ap.add_argument("--max-turns", type=int, default=250)
    ap.add_argument("--max-hours", type=float, default=48.0)
    ap.add_argument("--turn-timeout", type=float, default=7200)
    ap.add_argument("--max-restarts", type=int, default=6,
                    help="new sessions after lost runtimes (infrastructure; the claude_cli backend resumes its "
                         "session in a new process every turn)")
    ap.add_argument("--max-outage-hours", type=float, default=API_MAX_OUTAGE_H,
                    help="stop after this many hours of consecutive API failures (break time is not run time)")
    ap.add_argument("--poll", type=float, default=20.0)
    ap.add_argument("--message", help="first message (default: kickoff)")
    ap.add_argument("--stop-after-turns", type=int, help="stop after this many turns (preflight)")
    a = ap.parse_args()
    st = asyncio.run(main_async(a))
    print(json.dumps(st, indent=1, default=str))


if __name__ == "__main__":
    main()
