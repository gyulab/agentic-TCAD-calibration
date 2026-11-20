"""Command-line tools used by the agent (run from the workspace root as ``python3 tools/<name>.py``).

Every tool prints one JSON document on stdout and logs the call to logs/tool_events.jsonl.
Tools that run Sentaurus submit a background job and return its id; the agent then ends its
turn with ``WAIT_JOB <id>`` and is resumed by the orchestrator when the job finishes.
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path
from typing import Any, Callable

from . import algorithms as A
from . import config as C
from . import deck as D
from . import evaluation as E
from . import jobs as J
from . import proposals as P
from .state import StateError, Workspace, accept, activation_status
from .util import append_jsonl, dumps, now_utc, read_json, read_jsonl


def _ws() -> Workspace:
    return Workspace(Path.cwd())


def _emit(tool: str, argv: list[str], fn: Callable[[], Any]) -> int:
    ok, out = True, None
    try:
        out = fn()
    except (StateError, ValueError, FileNotFoundError, D.DeckGuardError, KeyError) as exc:
        ok, out = False, {"error": f"{type(exc).__name__}: {exc}"}
    print(dumps(out))
    try:
        ws = _ws()
        summary = dumps(out, indent=None)
        append_jsonl(ws.root / "logs" / "tool_events.jsonl", {"utc": now_utc(), "tool": tool, "argv": argv, "ok": ok,
                                                             "result": summary[:4000]})
    except Exception:  # noqa: BLE001
        pass
    return 0 if ok else 1


def _inert_note(ws: Workspace, deck: str, names: list[str]) -> str:
    reg = ws.registry()["params"]
    inert = [n for n in names if n in reg]
    if not inert:
        return ""
    return (f". {inert} are registered, but their tokens appear in no file of deck {deck}, so they have no effect on it. "
            "A model-change proposal whose edits place the tokens makes them parameters of the new deck (registered "
            "parameters need no new_params entry; their registered bounds apply)")


def _parse_sets(ws: Workspace, deck: str, sets: list[str]) -> dict[str, float]:
    spec = ws.params_for(deck)
    values = ws.base_values(deck)
    for s in sets or []:
        if "=" not in s:
            raise ValueError(f"--set expects NAME=VALUE, got {s!r}")
        k, v = s.split("=", 1)
        if k not in spec:
            raise ValueError(f"{k} is not a parameter of deck {deck}; parameters: {sorted(spec)}{_inert_note(ws, deck, [k])}")
        if k == "WF_gate" and ws.alignment_loss in C.GATE_FREE_LOSSES:
            raise StateError("WF_gate is set by the gate alignment after every simulation; it is not set by hand")
        if spec[k].get("lo") is None:
            raise StateError(f"{k} has no approved bounds yet (G1); only the start card can be evaluated")
        values[k] = float(v)
    return values


def _deck(ws: Workspace, deck: str | None) -> str:
    deck = deck or ws.accepted()["deck"]
    if deck not in ws.deck_versions():
        raise ValueError(f"unknown deck version {deck}; available {ws.deck_versions()}")
    return deck


def _job_note(jid: str) -> dict[str, Any]:
    return {"job": jid, "next": f"End your turn with the line: WAIT_JOB {jid}"}


# ---------------------------------------------------------------------------
def main_kb_search(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="kb_search", description="BM25 search of the Sentaurus Device User Guide (W-2024.09-SP1)")
    ap.add_argument("query", nargs="?")
    ap.add_argument("-k", type=int, default=8)
    ap.add_argument("--show", help="print the full text of one chunk id (e.g. p0573)")
    a = ap.parse_args(argv)

    def run() -> Any:
        from .kb import BM25
        kb = BM25(_ws().root / "kb" / "manual")
        if a.show:
            c = kb.get(a.show)
            if c is None:
                raise ValueError(f"unknown chunk {a.show}")
            return c
        if not a.query:
            raise ValueError("give a query or --show <chunk>")
        return {"query": a.query, "hits": kb.search(a.query, a.k)}
    return _emit("kb_search", argv, run)


def main_evaluate(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="evaluate", description="Run Sentaurus on the two calibration curves and score J (background job)")
    ap.add_argument("--deck", help="deck version (default: accepted)")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--label", default="")
    ap.add_argument("--show", metavar="EVAL_ID", help="show a finished evaluation instead of running one")
    a = ap.parse_args(argv)

    def run() -> Any:
        ws = _ws()
        if a.show:
            return E.eval_view(E.load_eval(ws, a.show))
        deck = _deck(ws, a.deck)
        values = _parse_sets(ws, deck, a.set)
        return _job_note(J.submit(ws, "evaluate", {"deck": deck, "values": values, "label": a.label}))
    return _emit("evaluate", argv, run)


def _param_list(ws: Workspace, deck: str, text: str) -> list[str]:
    params = [g.strip() for g in text.split(",") if g.strip()]
    spec = ws.params_for(deck)
    bad = [g for g in params if g not in spec]
    if bad:
        raise ValueError(f"not parameters of {deck}: {bad}; parameters: {sorted(spec)}{_inert_note(ws, deck, bad)}")
    fx = [g for g in params if g in ws.fixed]
    if fx:
        raise ValueError(f"{fx} fixed at the reference values (run rule fixed_work_functions): not fitting parameters")
    if "WF_gate" in params:
        raise ValueError("WF_gate is not a fitting parameter: the gate work function is set after every simulation by "
                         "aligning the simulated and measured curves along V_G (it only shifts the transfer curve)")
    unb = ws.bounds_ready(list(spec))
    if unb:
        raise StateError(f"no approved bounds for {unb}")
    return params


def main_ofat(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ofat", description="Probe runs: one run per parameter around the "
                                 "accepted card, reported as changes of V_th, SS, I_on, E_lin and region residuals")
    ap.add_argument("--params", "--group", dest="params", required=True, help="comma-separated parameter names")
    ap.add_argument("--deck")
    a = ap.parse_args(argv)

    def run() -> Any:
        ws = _ws()
        deck = _deck(ws, a.deck)
        params = _param_list(ws, deck, a.params)
        return _job_note(J.submit(ws, "ofat", {"deck": deck, "params": params, "values": ws.base_values(deck),
                                               "h": C.DELTA_P}))
    return _emit("ofat", argv, run)


def main_dfols(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="dfols", description="DFO-LS on the selected parameters jointly, "
                                 "repeated while a call improves J by >= 2 %; ends with a stall report (background job)")
    ap.add_argument("--params", required=True, help="comma-separated parameter names (moved jointly)")
    ap.add_argument("--deck")
    ap.add_argument("--recheck", action="store_true",
                    help="re-check: one call from a new probe design (step 0.2), for a stall without evidence")
    ap.add_argument("--from", dest="from_eval", help="start from this stored evaluation (same deck)")
    a = ap.parse_args(argv)

    def run() -> Any:
        ws = _ws()
        deck = _deck(ws, a.deck)
        params = _param_list(ws, deck, a.params)
        values, start = ws.base_values(deck), "the accepted card"
        if a.from_eval:
            e = E.load_eval(ws, a.from_eval)
            if e.get("deck") != deck or e.get("status") != "ok":
                raise ValueError(f"{a.from_eval} is not a converged evaluation of deck {deck}")
            values, start = {**values, **{k: v for k, v in e["values"].items() if k in values}}, a.from_eval
        else:
            prev = J.latest_unfinished_fit(ws, deck)
            acc = ws.accepted()
            if prev and prev.get("best_eval") and (acc.get("J") is None or prev["best_J"] < acc["J"]):
                e = E.load_eval(ws, prev["best_eval"])
                values = {**values, **{k: v for k, v in e["values"].items() if k in values}}
                start = f"{prev['best_eval']} (best point of {prev['job']}, which ended {prev['state']})"
        return {**_job_note(J.submit(ws, "dfols", {"deck": deck, "params": params, "values": values,
                                                   "recheck": bool(a.recheck), "start": start})),
                "start": start, "recheck": bool(a.recheck),
                "simulations_per_call_max": A.default_maxfun(len(params)),
                "calls_max": 1 if a.recheck else C.DFOLS_MAX_CALLS,
                "step_radius_start": C.SAFEGUARD_STEP if a.recheck else C.DELTA_P, "step_radius_end": C.RHO_END}
    return _emit("dfols", argv, run)


def main_deck_patch(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="deck_patch", description="Apply an APPROVED model_patch proposal, then smoke-test it")
    ap.add_argument("--proposal", help="approved proposal id, e.g. P002")
    ap.add_argument("--show", metavar="VERSION", help="print the templates of a deck version")
    ap.add_argument("--diff", metavar="VERSION", help="unified diff of a deck version against its parent")
    a = ap.parse_args(argv)

    def run() -> Any:
        ws = _ws()
        if a.show:
            return {"version": a.show, **D.read_templates(_deck_dir(ws, a.show))}
        if a.diff:
            reg = ws.registry()["decks"][a.diff]
            par = reg["parent"]
            if par is None:
                return {"version": a.diff, "diff": "", "note": "start deck"}
            t0, t1 = D.read_templates(ws.deck_dir(par)), D.read_templates(ws.deck_dir(a.diff))
            diff = "".join("".join(difflib.unified_diff(t0[k].splitlines(True), t1[k].splitlines(True), f"{par}/{D.FILES[k]}",
                                                        f"{a.diff}/{D.FILES[k]}")) for k in D.FILES)
            return {"version": a.diff, "parent": par, "diff": diff}
        if not a.proposal:
            raise ValueError("give --proposal, --show or --diff")
        res = P.apply_patch(ws, a.proposal)
        values = ws.base_values(res["deck"])
        jid = J.submit(ws, "smoke", {"deck": res["deck"], "values": values, "label": f"smoke {a.proposal}"})
        return {**res, **_job_note(jid)}
    return _emit("deck_patch", argv, run)


def _deck_dir(ws: Workspace, v: str) -> Path:
    if v not in ws.deck_versions():
        raise ValueError(f"unknown deck version {v}")
    return ws.deck_dir(v)


def main_state(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="state", description="Calibration state: show, history, propose, set-bounds, accept")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    h = sub.add_parser("history")
    h.add_argument("-n", type=int, default=30)
    p = sub.add_parser("propose", help="validate proposals/<ID>.json before requesting approval")
    p.add_argument("pid")
    b = sub.add_parser("set-bounds", help="apply an approved bounds proposal")
    b.add_argument("pid")
    c = sub.add_parser("accept", help="accept an evaluation with its step record (converged, J lower by >= 1e-3, "
                                      "targeted residuals smaller)")
    c.add_argument("eval_id")
    c.add_argument("--record", required=True, help="notes/steps/<name>.json with the step record")
    c.add_argument("--reason", default="")
    r = sub.add_parser("reject", help="record a rejected trial with its reason")
    r.add_argument("eval_or_job")
    r.add_argument("--reason", required=True)
    fz = sub.add_parser("freeze", help="freeze the accepted card after an approved final proposal")
    fz.add_argument("pid")
    a = ap.parse_args(argv)

    def run() -> Any:
        ws = _ws()
        if a.cmd == "show":
            return _show(ws)
        if a.cmd == "history":
            return read_jsonl(ws.state / "history.jsonl")[-a.n:]
        if a.cmd == "propose":
            from .kb import BM25
            acc = ws.accepted()
            ev = E.load_eval(ws, acc["eval"]) if acc.get("eval") else None
            res = P.validate(ws, a.pid, ev, BM25(ws.root / "kb" / "manual"))
            if res["ok"]:
                res["next"] = f"End your turn with the line: APPROVAL_REQUIRED {a.pid}"
            else:
                rep = _repeated_validation_error(ws)
                if rep:
                    res["repeated_error"] = rep
            return res
        if a.cmd == "set-bounds":
            return P.apply_bounds(ws, a.pid)
        if a.cmd == "accept":
            ev = E.load_eval(ws, a.eval_id)
            if ev["status"] != "ok":
                raise StateError(f"{a.eval_id} status {ev['status']}")
            rp = (ws.root / a.record).resolve()
            if ws.root / "notes" not in rp.parents:
                raise StateError("the step record must be a file under notes/")
            record = read_json(rp)
            if not isinstance(record, dict):
                raise StateError(f"{a.record} is not a JSON object")
            return accept(ws, ev, a.reason or record.get("action", ""), record)
        if a.cmd == "reject":
            ws.log("reject", target=a.eval_or_job, reason=a.reason)
            return {"recorded": a.eval_or_job}
        if a.cmd == "freeze":
            prop = read_json(P.proposal_path(ws, a.pid))
            if prop.get("type") != "final":
                raise StateError(f"{a.pid} is not a final proposal")
            ap_ = P.approval(ws, a.pid)
            acc = ws.accepted()
            ws.log("freeze", proposal=a.pid, accepted=acc, approval=ap_)
            (ws.state / "FROZEN").write_text(dumps({"proposal": a.pid, "accepted": acc, "utc": now_utc()}))
            return {"frozen": acc, "next": "End your turn with the line: FINAL_DONE"}
        raise ValueError(a.cmd)
    return _emit("state", argv, run)


def _show(ws: Workspace) -> dict[str, Any]:
    acc = ws.accepted()
    reg = ws.registry()
    ev = E.load_eval(ws, acc["eval"]) if acc.get("eval") else None
    budget = read_json(ws.state / "budget.json")
    act = activation_status(ws, ev)
    stop = {"calibration_target_met": bool(ev and ev.get("calibration_target_met")),
            "budget_used_up": bool(budget.get("cap") is not None and budget["used"] >= budget["cap"]),
            "parameter_update_stalled": act["parameter_update_stalled"]}
    params = {n: {k: v for k, v in s.items() if k in ("unit", "scale", "lo", "hi", "since", "meaning", "init")}
              for n, s in reg["params"].items()}
    params.get("WF_gate", {})["note"] = "set by gate work-function alignment after every simulation (not fitted)"
    for n, v in ws.fixed.items():
        params.get(n, {})["note"] = f"fixed at {v:g} (run rule fixed_work_functions): not aligned, bounded or fitted"
    return {"accepted": acc, "accepted_eval": E.eval_view(ev) if ev else None, "parameters": params,
            "deck_versions": reg["decks"], "budget_sdevice_runs": budget, "physics_model_addition": act,
            "stop_conditions": stop, "frozen": (ws.state / "FROZEN").exists(),
            "objective": ws.objective,
            "tolerances": _tolerances(ws, ev),
            "run_rules": sorted(k for k, v in (ws.meta.get("rules") or {}).items() if v),
            "rules": {"accept_min_dJ": C.ACCEPT_MIN_DJ, "stall_rel_gain": C.STALL_REL_GAIN,
                      "step_record_required": True, "mechanism_check": "targeted residuals must shrink"},
            "step_log": "state/STEP_LOG.md", "recent_history": read_jsonl(ws.state / "history.jsonl")[-8:],
            **_invalid_decks(ws)}


REPEAT_ERROR_MIN = 3


def _repeated_validation_error(ws: Workspace) -> str | None:
    rows = []
    for f in (ws.root / "proposals").glob("P*.validation_history.jsonl"):
        pid = f.name.split(".")[0]
        rows += [(r.get("utc") or "", r.get("version") or 0, pid, r) for r in read_jsonl(f)]
    rows.sort(key=lambda x: (x[0], x[1]))
    streak, common = [], None
    for _, _, pid, r in reversed(rows):
        if r.get("ok"):
            break
        errs = set(r.get("errors") or [])
        nxt = errs if common is None else common & errs
        if not nxt:
            break
        common = nxt
        streak.append(pid)
    if len(streak) < REPEAT_ERROR_MIN or not common:
        return None
    pids = sorted(set(streak))
    err = sorted(common)[0]
    return (f"The same validation error has been returned on the last {len(streak)} validations in a row "
            f"({', '.join(pids[:6])}{' ...' if len(pids) > 6 else ''}): {err!r}. The check is deterministic: a proposal "
            "that keeps the part it refers to is refused again.")


def _invalid_decks(ws: Workspace) -> dict[str, Any]:
    bad: dict[str, str] = {}
    good: set[str] = set()
    for f in sorted((ws.root / "evals").glob("E*.json")):
        e = read_json(f) or {}
        if e.get("status") == "deck_error":
            msg = next((r["deck_error"].get("message") for r in e.get("runs", []) if r.get("deck_error")), "")
            bad.setdefault(e.get("deck"), f"{e.get('eval_id')}: {msg}")
        elif e.get("converged"):
            good.add(e.get("deck"))
    bad = {d: m for d, m in bad.items() if d not in good}
    if not bad:
        return {}
    return {"invalid_decks": {d: f"sdevice could not read this deck version ({m}). Write a corrected model_patch "
                                 "proposal." for d, m in bad.items()}}


def _tolerances(ws: Workspace, ev: dict | None) -> dict[str, Any]:
    tol = {"V_th_mV": 1e3 * C.TOL["dVth_V"], "SS_fraction": C.TOL["dSS_rel"],
           "log10_I_on_dec": C.TOL["dlogIon_dec"], "E_lin": C.TOL["E_lin"]}
    if ws.objective in C.TERM_OBJECTIVES:
        sh = (ev or {}).get("V_th_shift") or {}
        tol["V_th_shift_fraction_of_measured"] = C.TOL_VTH_SHIFT_REL
        if sh.get("tolerance_mV") is not None:
            tol["V_th_shift_mV"] = round(sh["tolerance_mV"], 1)
        tol["curve_points"] = {"V_G_mV": 1e3 * C.SIGMA_V, "current_dec": C.SIGMA_R}
        tol["target"] = "every J term <= 1"
    return tol


def main_jobs(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="jobs", description="Background job status and results")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("job")
    c = sub.add_parser("cancel")
    c.add_argument("job")
    a = ap.parse_args(argv)

    def run() -> Any:
        ws = _ws()
        if a.cmd == "list":
            d = ws.root / "jobs"
            return [J.status(ws, p.name) for p in sorted(d.iterdir()) if p.is_dir()] if d.exists() else []
        if a.cmd == "show":
            st = J.status(ws, a.job)
            res = read_json(ws.root / "jobs" / a.job / "result.json")
            if res and "trace" in res:
                res = {**res, "trace": res["trace"]}
            if res and "jacobian_unit" in res:
                res = {k: v for k, v in res.items() if k != "jacobian_unit"}
                res["note"] = "full Jacobian columns are in jobs/<id>/result.json"
            return {"status": st, "result": res}
        if a.cmd == "cancel":
            return J.cancel(ws, a.job)
        raise ValueError(a.cmd)
    return _emit("jobs", argv, run)


from .diagtools import main_diagnose, main_extract, main_identify, main_vd_consistency, main_verify  # noqa: E402

TOOLS = {"kb_search": main_kb_search, "evaluate": main_evaluate, "ofat": main_ofat, "dfols": main_dfols,
         "deck_patch": main_deck_patch, "state": main_state, "jobs": main_jobs,
         "verify": main_verify, "diagnose": main_diagnose, "vd_consistency": main_vd_consistency, "extract": main_extract,
         "identify": main_identify}
