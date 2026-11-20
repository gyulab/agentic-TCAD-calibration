"""Background jobs (evaluate / ofat / dfols / smoke). Each job is a detached process that writes
``jobs/<id>/status.json`` and ``result.json``. The orchestrator, not the LLM, waits for jobs."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from . import algorithms as A
from . import config as C
from . import evaluation as E
from . import objective as O
from . import sentaurus as S
from .state import Workspace, to_unit
from .util import append_jsonl, file_lock, now_utc, read_json, write_json

KINDS = ("evaluate", "ofat", "dfols", "smoke", "verify", "diag")


def _next_job_id(ws: Workspace) -> str:
    d = ws.root / "jobs"
    d.mkdir(exist_ok=True)
    with file_lock(d / ".id.lock"):
        c = read_json(d / ".counter.json", {"n": 0})
        c["n"] += 1
        write_json(d / ".counter.json", c)
    return f"J{c['n']:04d}"


def submit(ws: Workspace, kind: str, spec: Mapping[str, Any]) -> str:
    if kind not in KINDS:
        raise ValueError(kind)
    jid = _next_job_id(ws)
    jd = ws.root / "jobs" / jid
    jd.mkdir(parents=True)
    write_json(jd / "spec.json", {"job": jid, "kind": kind, "spec": dict(spec), "submitted_utc": now_utc()})
    write_json(jd / "status.json", {"job": jid, "kind": kind, "state": "queued", "utc": now_utc()})
    env = dict(os.environ, PYTHONPATH=str(C.HARNESS / "lib"))
    with open(jd / "job.log", "w") as log:
        p = subprocess.Popen([sys.executable, "-m", "tcadcal.jobrunner", str(ws.root), jid], stdout=log,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, env=env,
                             cwd=str(ws.root))
    st = read_json(jd / "status.json")
    st["pid"] = p.pid
    write_json(jd / "status.json", st)
    append_jsonl(ws.root / "logs" / "jobs.jsonl", {"utc": now_utc(), "job": jid, "kind": kind, "event": "submitted",
                                                   "spec": dict(spec)})
    return jid


def status(ws: Workspace, jid: str) -> dict[str, Any]:
    st = read_json(ws.root / "jobs" / jid / "status.json")
    if st is None:
        raise FileNotFoundError(f"no job {jid}")
    if st["state"] in ("queued", "running") and st.get("pid"):
        try:
            os.kill(st["pid"], 0)
        except ProcessLookupError:
            if read_json(ws.root / "jobs" / jid / "status.json")["state"] in ("queued", "running"):
                st["state"] = "died"
    return st


def cancel(ws: Workspace, jid: str) -> dict[str, Any]:
    st = status(ws, jid)
    if st["state"] in ("queued", "running") and st.get("pid"):
        try:
            os.killpg(st["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        st.update({"state": "cancelled", "utc": now_utc()})
        write_json(ws.root / "jobs" / jid / "status.json", st)
    return st


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def latest_unfinished_fit(ws: Workspace, deck: str) -> dict[str, Any] | None:
    """The latest fit job (dfols) on ``deck`` that was cancelled, died or failed, with its best evaluation."""
    for d in sorted((ws.root / "jobs").glob("J*"), reverse=True):
        st, sp = read_json(d / "status.json") or {}, read_json(d / "spec.json") or {}
        if sp.get("kind") != "dfols" or (sp.get("spec") or {}).get("deck") != deck:
            continue
        if st.get("state") in ("cancelled", "died", "failed"):
            return st
        if st.get("state") == "done":
            return None
    return None


def _progress(ws: Workspace, jid: str, **kw: Any) -> None:
    p = ws.root / "jobs" / jid / "status.json"
    st = read_json(p)
    st.update(kw)
    st["utc"] = now_utc()
    write_json(p, st)


def _budget(ws: Workspace) -> S.Budget:
    return S.Budget(ws.state / "budget.json", None)


def run_job(ws: Workspace, jid: str) -> dict[str, Any]:
    import multiprocessing
    if multiprocessing.parent_process() is not None:
        raise RuntimeError(f"run_job({jid}) called inside a worker process: guard the calling script with "
                           "`if __name__ == '__main__':`")
    spec_all = read_json(ws.root / "jobs" / jid / "spec.json")
    kind, spec = spec_all["kind"], spec_all["spec"]
    _progress(ws, jid, state="running", started_utc=now_utc(), pid=os.getpid())
    log = lambda s: print(s, flush=True)  # noqa: E731
    budget = _budget(ws)
    n_ev = {"n": 0}

    best = {"eval": None, "J": None}

    def ev(values: Mapping[str, float], label: str) -> dict[str, Any]:
        e = E.evaluate(ws, spec["deck"], values, label=label, job=jid, budget=budget)
        n_ev["n"] += 1
        if e.get("status") == "ok" and e.get("J") is not None and (best["J"] is None or e["J"] < best["J"]):
            best.update({"eval": e["eval_id"], "J": e["J"]})
        _progress(ws, jid, n_evals=n_ev["n"], last_eval=e["eval_id"], last_J=e.get("J"),
                  best_eval=best["eval"], best_J=best["J"])
        return e

    if kind == "evaluate":
        e = ev(spec["values"], spec.get("label", kind))
        return {"eval": E.eval_view(e)}
    if kind == "smoke":
        e = ev(spec["values"], spec.get("label", kind))
        out = {"eval": E.eval_view(e)}
        from . import deck as D
        from . import verify as V
        si = V.silicon_template_models(D.read_templates(ws.deck_dir(spec["deck"])))
        if si:
            out["silicon_template_models"] = si
        if e.get("status") == "ok" and not E.deck_flags(ws, spec["deck"]).get("verified") and not ws.rule("fixed_mesh"):
            out["verification"] = verify_card(ws, e["eval_id"], budget, log)
            out["eval"] = E.eval_view(E.load_eval(ws, e["eval_id"]))
        return out
    if kind == "verify":
        if ws.rule("fixed_mesh"):
            return {"error": FIXED_MESH_NOTE}
        return verify_card(ws, spec["eval"], budget, log)
    if kind == "diag":
        return diag_run(ws, spec["eval"], float(spec["vd"]), budget, log)
    if kind == "ofat":
        return _probes(ws, spec["deck"], spec["params"], dict(spec["values"]), float(spec.get("h", C.DELTA_P)), ev)
    if kind == "dfols":
        return _dfols(ws, jid, spec, ev, log)
    raise ValueError(kind)


FIXED_MESH_NOTE = ("this run uses one fixed mesh for the whole calibration (run rule fixed_mesh); the final card is "
                   "checked on the refined mesh by the user after the run, so no refined-mesh runs are made here")


def verify_card(ws: Workspace, eval_id: str, budget: S.Budget, log) -> dict[str, Any]:
    from . import deck as D
    from . import verify as V
    e = E.load_eval(ws, eval_id)
    if e.get("status") != "ok":
        return {"error": f"{eval_id} is not a converged evaluation ({e.get('status')})"}
    deck = e["deck"]
    spec = ws.params_for(deck)
    templates = D.read_templates(ws.deck_dir(deck))
    vals = dict(e.get("values_rendered") or e["values"])
    prod = {float(r["vd"]): S.load_curve(r) for r in e["runs"]}
    fine, runs = {}, []
    for dev, vd in ws.calibration:
        r = S.run_one(ws.store, templates, spec, vals, dev, vd, budget, mesh_scale=0.5, levels=[0, 1],
                      violations_log=ws.root / "logs" / "violations.jsonl", timeout_s=ws.meta.get("sdevice_timeout_s"),
                      budget_category="verification")
        runs.append({k: r.get(k) for k in ("vd", "ok", "reason", "recovery_name", "run_key", "validity")})
        if r.get("ok"):
            fine[float(vd)] = S.load_curve(r)
    ss_meas = {float(b["vd"]): b["SS_meas_mV_dec"] for b in e["branches"].values()}
    env_path = E.envelope_path(ws)
    env = read_json(env_path, {}) or {}
    d = env.setdefault(deck, {"verified": []})
    if len(fine) < len(prod):
        result = {"eval": eval_id, "pass": False, "reason": "the halved mesh did not converge", "runs": runs}
        d.setdefault("unverifiable", []).append({"eval": eval_id, "utc": now_utc(), "runs": runs})
    else:
        cmp = V.compare_curves(prod, fine, ss_meas)
        result = {"eval": eval_id, "pass": cmp["pass"], "comparison": cmp, "runs": runs}
        if cmp["pass"]:
            d["verified"].append({"eval": eval_id, "values": vals, "utc": now_utc(),
                                  "change_x3_indicator": cmp["change_x3_indicator"]})
        else:
            d["mesh_sensitive"] = {"eval": eval_id, "utc": now_utc(), "fail": cmp["fail"]}
    write_json(env_path, env)
    ws.log("verification", eval=eval_id, deck=deck, passed=result["pass"],
           fail=(result.get("comparison") or {}).get("fail") or result.get("reason"))
    ev = E.refresh_validity(ws, E.load_eval(ws, eval_id))
    result["validity_after"] = ev.get("validity")
    return result


def diag_run(ws: Workspace, eval_id: str, vd: float, budget: S.Budget, log) -> dict[str, Any]:
    """Extra diagnostic run (counted in the diagnostic runs inside the total budget): the card of an evaluation at one
    calibration V_D with snapshots every 75 mV of the gate sweep."""
    from . import deck as D
    e = E.load_eval(ws, eval_id)
    if e.get("status") != "ok":
        return {"error": f"{eval_id} is not a converged evaluation"}
    conds = [c for c in ws.calibration if abs(float(c[1]) - vd) < 1e-9]
    if not conds:
        return {"error": f"V_D {vd:g} V is not a calibration bias (only {sorted(float(c[1]) for c in ws.calibration)})"}
    deck = e["deck"]
    r = S.run_one(ws.store, D.read_templates(ws.deck_dir(deck)), ws.params_for(deck), dict(e.get("values_rendered") or e["values"]),
                  conds[0][0], vd, budget, levels=[0, 1], violations_log=ws.root / "logs" / "violations.jsonl",
                  timeout_s=ws.meta.get("sdevice_timeout_s"), budget_category="diagnostic", snap_spacing=C.DIAG_SNAP_SPACING)
    out = {"eval": eval_id, "vd": vd, "ok": r.get("ok"), "reason": r.get("reason"), "run_dir": r.get("run_dir"),
           "n_snapshots": r.get("n_snapshots")}
    if r.get("ok") and r.get("run_dir"):
        out["snapshots"] = str(Path(r["run_dir"]) / "snapshots.json.gz")
    return out


def numerics_of_traces(traces: list[dict[str, Any]], best_x: list[float] | None) -> dict[str, Any]:
    """How numerically clean a fit was: the fraction of converged points and failures next to the best point (within
    the trust radius in scaled units). A stall with failures is not evidence of missing physics."""
    n = len(traces)
    failed = [t for t in traces if t.get("status") != "ok"]
    near = []
    if best_x is not None:
        bx = np.asarray(best_x, float)
        for t in failed:
            if len(t.get("x", [])) == len(bx) and float(np.max(np.abs(np.asarray(t["x"]) - bx))) <= C.DELTA_P + 1e-12:
                near.append(t["eval"])
    frac = (n - len(failed)) / n if n else 1.0
    return {"n_points": n, "n_failed": len(failed), "valid_fraction": round(frac, 3), "failures_near_best": near,
            "numerically_limited": bool(frac < C.PHYSICS_STALL_MIN_VALID or near)}


# ---------------------------------------------------------------------------
# Device-term snapshots of an evaluation and probe effects
# ---------------------------------------------------------------------------
def snapshot(e: Mapping[str, Any]) -> dict[str, Any]:
    """Device metrics and region residuals of one evaluation, per drain bias."""
    out = {}
    for k, b in e["branches"].items():
        vd = f"VD={b['vd']:g}V"
        ss_pct = None if b["dSS_V_dec"] is None or not b["SS_meas_mV_dec"] else 100 * 1e3 * b["dSS_V_dec"] / b["SS_meas_mV_dec"]
        out[vd] = {"dV_th_mV": None if b["dVth_V"] is None else 1e3 * b["dVth_V"], "dSS_percent": ss_pct,
                   "dlog10_I_on_dec": b["dlogIon_dec"], "E_lin": b["E_lin"],
                   "region_mean_r2": {r: b["regions"][r].get("mean_r2") for r in O.REGIONS}}
        if e.get("objective") in C.TERM_OBJECTIVES:
            out[vd]["curve_terms"] = {r: t["value"] for r, t in (b.get("curve_terms") or {}).items()}
    return out


def shift_snapshot(e: Mapping[str, Any]) -> dict[str, Any] | None:
    sh = e.get("V_th_shift")
    if not sh:
        return None
    return {k: (None if sh.get(k) is None else round(float(sh[k]), 2))
            for k in ("TCAD_mV", "measured_mV", "error_mV", "tolerance_mV", "error")}


def _diff(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    out = {}
    for vd in a:
        d = {}
        for k in ("dV_th_mV", "dSS_percent", "dlog10_I_on_dec", "E_lin"):
            if a[vd][k] is not None and b[vd][k] is not None:
                d[f"change_of_{k}"] = round(b[vd][k] - a[vd][k], 4)
        d["change_of_region_mean_r2"] = {r: (None if a[vd]["region_mean_r2"][r] is None or b[vd]["region_mean_r2"][r] is None
                                             else round(b[vd]["region_mean_r2"][r] - a[vd]["region_mean_r2"][r], 3))
                                         for r in O.REGIONS}
        if "curve_terms" in a[vd] and "curve_terms" in b[vd]:
            d["change_of_curve_terms"] = {r: round(b[vd]["curve_terms"][r] - v, 3) for r, v in a[vd]["curve_terms"].items()
                                          if r in b[vd]["curve_terms"]}
        out[vd] = d
    return out


def _shift_diff(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not a or not b or a.get("TCAD_mV") is None or b.get("TCAD_mV") is None:
        return None
    return {"change_of_TCAD_V_th_shift_mV": round(b["TCAD_mV"] - a["TCAD_mV"], 2),
            "change_of_error_in_tolerance_units": round(b["error"] - a["error"], 3)}


def _probes(ws: Workspace, deck: str, params: list[str], base: dict, h: float, ev) -> dict[str, Any]:
    from .state import from_unit
    pspec = ws.params_for(deck)
    e0 = ev(base, "probe base")
    if e0["status"] != "ok":
        return {"error": f"base evaluation {e0['eval_id']} failed: {e0['status']}", "base": E.eval_view(e0)}
    base = dict(e0["values"])
    s0, sh0 = snapshot(e0), shift_snapshot(e0)
    R0 = np.asarray(e0["residual_vector"], float)
    R0u = None if e0.get("residual_vector_unweighted") is None else np.asarray(e0["residual_vector_unweighted"], float)
    x0 = {n: to_unit(pspec[n], base[n]) for n in params}
    cols, cols_u, probes = {}, {}, []
    for n in params:
        xp = A.probe_point(x0[n], h)
        vals = dict(base)
        vals[n] = from_unit(pspec[n], xp)
        e = ev(vals, f"probe {n}")
        first_fail, xm = None, 2 * x0[n] - xp
        if e["status"] != "ok" and 0.0 <= xm <= 1.0:
            first_fail, xp = e["eval_id"], xm
            vals[n] = from_unit(pspec[n], xp)
            e = ev(vals, f"probe {n} (mirror step after a numerical failure)")
        step = xp - x0[n]
        rec = {"param": n, "meaning": pspec[n].get("meaning"), "value_base": base[n], "value_probe": e["values"][n],
               "step_in_bound_range": round(step, 4), "eval": e["eval_id"], "status": e["status"], "J": e.get("J")}
        if first_fail:
            rec["failed_probe_before_mirror"] = first_fail
        if e["status"] == "ok":
            cols[n] = (np.asarray(e["residual_vector"], float) - R0) / step
            if R0u is not None and e.get("residual_vector_unweighted") is not None:
                cols_u[n] = (np.asarray(e["residual_vector_unweighted"], float) - R0u) / step
            rec["change_of_J"] = round(e["J"] - e0["J"], 5)
            rec["change_of_J_terms_in_tolerance_units"] = _term_error_change(e0, e)
            fx, wf0, src = fixed_wf_record(e, (e0.get("values") or {}).get("WF_gate"))
            if fx is not None:
                rec["change_at_fixed_gate_work_function_in_tolerance_units"] = _term_error_change(
                    e0, {"summary": {"J_terms": fx["J_terms"]}})
                rec["change_of_J_at_fixed_gate_work_function"] = round(fx["J"] - e0["J"], 5)
                rec["fixed_wf_source"] = src
            else:
                rec["change_at_fixed_gate_work_function_in_tolerance_units"] = None
                rec["fixed_gate_work_function_note"] = ("the probe was simulated at another gate work function than the "
                                                       "base's aligned value (re-centring or no alignment)")
            rec["validity"] = e.get("validity")
            rec["effect"] = _diff(s0, snapshot(e))
            if sh0:
                rec["effect"]["V_th_shift"] = _shift_diff(sh0, shift_snapshot(e))
            rec["gate_work_function_after_alignment_eV"] = e["values"].get("WF_gate")
            if e.get("bands") and e0.get("bands"):     # What the probe changed inside the device
                from . import bands as BD
                rec["inside_change"] = BD.change(e0["bands"], e["bands"])
        else:
            cols[n] = np.full_like(R0, np.nan)
        probes.append(rec)
    rank = A.ofat_rank(R0, cols, x0, C.DELTA_P, max_group=len(params))
    from . import verify as V
    sens = V.sensitivity_labels(probes, e0.get("activation"))
    return {"deck": deck, "base_eval": e0["eval_id"], "J_base": e0["J"], "base_metrics": s0, "base_V_th_shift": sh0,
            "base_inside_the_device": e0.get("bands"),
            "probes": probes,
            "activation_and_sensitivity": {n: {"activation": (e0.get("activation") or {}).get(n, {}).get("label"),
                                               "activation_basis": (e0.get("activation") or {}).get(n, {}).get("why"),
                                               **sens.get(n, {}),
                                               "max_change_after_realignment_tol": _max_abs(
                                                   next((p.get("change_of_J_terms_in_tolerance_units") for p in probes
                                                         if p["param"] == n), None)),
                                               "max_change_at_fixed_gate_work_function_tol": _max_abs(
                                                   next((p.get("change_at_fixed_gate_work_function_in_tolerance_units")
                                                         for p in probes if p["param"] == n), None))} for n in params},
            "sensitivity_note": ("Sensitivity after re-alignment is what J sees. A parameter that moves the curves at a fixed "
                                 "gate work function but not after re-alignment is degenerate with the alignment: it may be "
                                 "deprioritized for now, but not fixed, and not called physically unimportant."),
            "identifiability": identifiability(cols, x0),
            "identifiability_unweighted": identifiability(cols_u, x0) if cols_u else
            {"note": "no unweighted residuals for this base evaluation (not stored for this evaluation)"},
            "identifiability_note": ("`identifiability` uses the residuals of the run objective: it shows which directions the search can move. `identifiability_unweighted` "
                                     "uses the tolerance-normalized residuals of all J terms with equal term weights: it "
                                     "shows parameter sensitivity and correlation in the measured data. Neither proves that "
                                     "a parameter is uniquely identified."),
            "predicted_J_after_joint_update": [{"params": r["group"], "J_now": round(r["J_now"], 4),
                                                "J_predicted_within_bounds": round(r["J_pred_within_bounds"], 4)}
                                               for r in rank[:12]],
            "jacobian_unit": {n: c.tolist() for n, c in cols.items()}, "base_values": base}


def fixed_wf_record(e: Mapping[str, Any], wf0: float | None) -> tuple[dict | None, float | None, str | None]:
    if wf0 is None:
        return None, None, None
    for key, src in (("at_rendered_gate_work_function", "final simulation"),
                     ("at_first_rendered_gate_work_function", "first simulation, before re-centring")):
        fx = e.get(key)
        if fx and abs(float(fx["WF_gate_eV"]) - float(wf0)) < 1e-4:
            return fx, wf0, src
    return None, wf0, None


def _max_abs(d: Mapping[str, float] | None) -> float | None:
    vals = [abs(v) for v in (d or {}).values() if v is not None]
    return round(max(vals), 4) if vals else None


def _term_error_change(e0: Mapping[str, Any], e1: Mapping[str, Any]) -> dict[str, float]:
    """Change of each J term's signed error in tolerance units (region terms: change of the RMS r_i)."""
    def errs(e):
        t = ((e.get("summary") or {}).get("J_terms") or {}).get("terms") or {}
        out = {}
        for k, v in t.items():
            err = v.get("error")
            out[k] = float(err) if err is not None else (float(v["term"]) ** 0.5 if v.get("term") is not None else None)
        return out
    a, b = errs(e0), errs(e1)
    return {k: round(b[k] - a[k], 4) for k in a if k in b and a[k] is not None and b[k] is not None}


def identifiability(cols: Mapping[str, np.ndarray], x0: Mapping[str, float]) -> dict[str, Any]:
    """Local identifiability of the probed parameters from the probe Jacobian of the J residual vector (scaled units):
    singular values, condition number, collinear parameter pairs (|cos| > 0.95) and the weakest direction. A collinear
    pair cannot be separated by these two curves: report it as a combination, not as two constrained values."""
    names = [n for n in cols if np.all(np.isfinite(cols[n]))]
    if len(names) < 1:
        return {"note": "no converged probe"}
    S_ = np.column_stack([cols[n] for n in names])
    norms = np.linalg.norm(S_, axis=0)
    out: dict[str, Any] = {"params": names, "column_norm": {n: round(float(v), 4) for n, v in zip(names, norms)}}
    if len(names) >= 2:
        u, sv, vt = np.linalg.svd(S_, full_matrices=False)
        out["singular_values"] = [round(float(v), 5) for v in sv]
        out["condition_number"] = None if sv[-1] == 0 else round(float(sv[0] / sv[-1]), 2)
        out["weakest_direction"] = {n: round(float(c), 3) for n, c in zip(names, vt[-1])}
        pairs = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                if norms[i] > 0 and norms[j] > 0:
                    c = float(S_[:, i] @ S_[:, j] / (norms[i] * norms[j]))
                    if abs(c) > 0.95:
                        pairs.append({"pair": [names[i], names[j]], "cos": round(c, 4)})
        out["collinear_pairs"] = pairs
    return out


def _seeds(ws: Workspace, deck: str, ref: Mapping[str, float], params: list[str]) -> list[dict]:
    """Every earlier evaluation of this deck version with the same values of the fixed parameters."""
    pspec = ws.params_for(deck)
    fixed = [n for n in pspec if n not in params and n != "WF_gate"]
    out = []
    for f in sorted((ws.root / "evals").glob("E*.json")):
        e = read_json(f)
        if not e or e.get("status") != "ok" or e.get("deck") != deck:
            continue
        if all(abs(e["values"][n] - ref[n]) <= 1e-6 * max(1.0, abs(ref[n])) for n in fixed):
            out.append(e)
    return out


def _term_errors(e: Mapping[str, Any]) -> dict[str, float]:
    """Signed error of every J term in tolerance units (region terms: RMS r_i, unsigned)."""
    t = ((e.get("summary") or {}).get("J_terms") or {}).get("terms") or {}
    out = {}
    for k, v in t.items():
        err = v.get("error")
        out[k] = float(err) if err is not None else (float(v["term"]) ** 0.5 if v.get("term") is not None else None)
    return {k: v for k, v in out.items() if v is not None}


def stall_report(ws: Workspace, jid: str, params: list[str], evals: list[Mapping[str, Any]], best: Mapping[str, Any],
                 cost: Mapping[str, Any]) -> dict[str, Any]:
    from .state import to_unit
    pspec = ws.params_for(best["deck"])
    ok = [e for e in evals if e.get("status") == "ok" and e.get("J") is not None]
    js = [e["J"] for e in ok]
    best_so_far = list(np.minimum.accumulate(js)) if js else []
    n = C.STALL_TREND_N
    trend = {"n_evaluations": len(ok), "J_best": best["J"],
             "J_best_n_ago": best_so_far[-n - 1] if len(best_so_far) > n else (best_so_far[0] if best_so_far else None),
             "last_n": n}
    if trend["J_best_n_ago"]:
        trend["relative_improvement_last_n"] = round((trend["J_best_n_ago"] - best["J"]) / trend["J_best_n_ago"], 4)
    terms_best = (best.get("J_terms") or {})
    failing = {k: round(v, 4) for k, v in terms_best.items() if v > 1.0}
    near = [e for e in ok if e["J"] <= C.STALL_NEAR_FACTOR * best["J"]]
    errs = [_term_errors(e) for e in near]
    tradeoffs = []
    names = sorted(failing)
    if len(near) >= C.STALL_MIN_POINTS:
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                xa = np.array([d.get(a, np.nan) for d in errs], float)
                xb = np.array([d.get(b, np.nan) for d in errs], float)
                m = np.isfinite(xa) & np.isfinite(xb)
                if m.sum() >= C.STALL_MIN_POINTS and np.std(xa[m]) > 0 and np.std(xb[m]) > 0:
                    r = float(np.corrcoef(xa[m], xb[m])[0, 1])
                    if r <= C.STALL_TRADEOFF_CORR:
                        tradeoffs.append({"terms": [a, b], "correlation": round(r, 3), "n_points": int(m.sum())})
    movable = {}
    if len(near) >= len(params) + 2:
        X = np.array([[to_unit(pspec[p], float(e["values"][p])) for p in params] for e in near], float)
        x0 = np.array([to_unit(pspec[p], float(best["values"][p])) for p in params], float)
        A = np.column_stack([np.ones(len(near)), X - x0])
        eb = _term_errors(best)
        for t in names:
            y = np.array([d.get(t, np.nan) for d in errs], float)
            m = np.isfinite(y)
            if m.sum() < len(params) + 2:
                continue
            coef, *_ = np.linalg.lstsq(A[m], y[m], rcond=None)
            grad = coef[1:]
            e0 = eb.get(t, float(np.sqrt(failing[t])))
            gains = {}
            for j, p in enumerate(params):
                step = -np.sign(e0 * grad[j]) * C.DELTA_P   # the direction that reduces |error|
                room = (1.0 - x0[j]) if step > 0 else x0[j]
                gains[p] = round(float(min(abs(step), room) * abs(grad[j])), 4)
            movable[t] = {"error": round(e0, 3), "reduction_per_trust_step_tol": gains,
                          "movable": bool(max(gains.values(), default=0.0) >= C.STALL_MOVABLE_TOL)}
    evidence = []
    if tradeoffs:
        evidence.append("trade-off between terms outside tolerance")
    if movable and not any(v["movable"] for v in movable.values()):
        evidence.append(f"no fitted parameter moves a term outside tolerance by >= {C.STALL_MOVABLE_TOL:g} tolerance "
                        "within one trust step (regression on this fit's evaluations)")
    return {"trend": trend, "terms_outside_tolerance": failing,
            "tradeoffs": tradeoffs, "sensitivity": movable,
            "evidence_for_a_parameter_limit": evidence, "cost": dict(cost),
            "note": "Model predictions of the simulated device, from this fit's own evaluations. They do not prove that "
                    "a model family cannot fix a term."}


def _dfols(ws: Workspace, jid: str, spec: Mapping[str, Any], ev, log) -> dict[str, Any]:
    import time
    deck, params = spec["deck"], list(spec["params"])
    pspec = ws.params_for(deck)
    t_start, used0 = time.time(), _budget(ws).used().get("used", 0)
    e0 = ev(dict(spec["values"]), "dfols start" + (" (re-check)" if spec.get("recheck") else ""))
    if e0["status"] != "ok":
        return {"error": f"start evaluation {e0['eval_id']} failed: {e0['status']}"}
    cur, calls, stalled, call_log = e0, 0, False, []
    all_traces: list[dict[str, Any]] = []
    maxfun = A.default_maxfun(len(params))
    recheck = bool(spec.get("recheck"))
    while calls < (1 if recheck else C.DFOLS_MAX_CALLS):
        if recheck:
            probes = _probes(ws, deck, params, dict(cur["values"]), C.SAFEGUARD_STEP, ev)
            seeds = [E.load_eval(ws, p["eval"]) for p in probes.get("probes", []) if p["status"] == "ok"]
            rhobeg = C.SAFEGUARD_STEP
        else:
            seeds, rhobeg = _seeds(ws, deck, cur["values"], params), C.DELTA_P
        res = A.run_dfols(ev, pspec, dict(cur["values"]), params, cur, seeds, rhobeg=rhobeg, rhoend=C.RHO_END,
                          maxfun=maxfun, log=log)
        all_traces += res.get("trace", [])
        calls += 1
        best = E.load_eval(ws, res["best_eval"]) if res["best_eval"] else cur
        gain = (cur["J"] - best["J"]) / cur["J"]
        call_log.append({"call": calls, "J_start": cur["J"], "J_best": best["J"], "rel_gain": gain, "n_evals": res["n_evals"],
                         "seeds_used": len(res["seed_evals"]), "dfols_exit": res["dfols_msg"],
                         **({"recheck_probe_step": C.SAFEGUARD_STEP} if recheck else {})})
        cur = best if best["J"] < cur["J"] else cur
        if gain < C.STALL_REL_GAIN:
            stalled = True
            break
    best_x = next((t["x"] for t in all_traces if t.get("eval") == cur["eval_id"]), None)
    numerics = numerics_of_traces(all_traces, best_x)
    target = bool(cur.get("calibration_target_met"))
    job_evals = [E.load_eval(ws, t["eval"]) for t in all_traces if t.get("eval")]
    cost = {"tcad_runs": _budget(ws).used().get("used", 0) - used0, "evaluations": len(job_evals) + 1,
            "wall_min": round((time.time() - t_start) / 60, 1)}
    report = stall_report(ws, jid, params, [e0] + job_evals, cur, cost)
    stall_condition = bool(stalled and not target and not numerics["numerically_limited"])
    ready = bool(stall_condition and (report["evidence_for_a_parameter_limit"] or recheck))
    if target:
        options = ["the calibration target is met: propose the final model"]
    elif not stalled:
        options = ["continue: the last call still improved J by >= 2 % (submit dfols again from the best point)"]
    elif numerics["numerically_limited"]:
        options = ["numerically limited: follow the numerical-recovery skill; this stall is not evidence of missing physics"]
    else:
        options = ["end this search and accept the best evaluation as a step",
                   "re-check from a new probe design (dfols --recheck, about "
                   f"{len(params) + 1 + A.default_maxfun(len(params))} evaluations)",
                   "propose a model change (model_patch): " + ("open now, with the evidence in the stall report"
                                                               if ready else "open after a re-check, or on a reviewer waiver")]
    out = {"deck": deck, "params": params, "start_eval": e0["eval_id"], "J_start": e0["J"], "best_eval": cur["eval_id"],
           "J_best": cur["J"], "rel_gain": (e0["J"] - cur["J"]) / e0["J"], "recheck": recheck,
           "acceptable": bool(e0["J"] - cur["J"] >= C.ACCEPT_MIN_DJ), "calls": call_log,
           "stall_condition_met": stall_condition, "model_addition_ready": ready,
           "stall_confirmed": ready,
           "stall_numerically_limited": bool(stalled and numerics["numerically_limited"]),
           "stall_report": report, "options": options,
           "numerics": numerics, "best_validity": cur.get("validity"), "calibration_target_met": target,
           "metrics_start": snapshot(e0), "metrics_best": snapshot(cur),
           "V_th_shift_start": shift_snapshot(e0), "V_th_shift_best": shift_snapshot(cur), "best": E.eval_view(cur),
           "rule": "a call is repeated while it improves J by >= 2 %. stall_condition_met = the last call "
                   "improved J by < 2 %, the fit was numerically clean and the target is not met. A model change is "
                   "ready when the stall report shows evidence (a trade-off between terms outside tolerance, or no "
                   "fitted parameter that still moves them), after an explicit re-check, or on a recorded reviewer "
                   "waiver. A model change is still reviewed and verified on its own."}
    ws.log("dfols_result", job=jid, deck=deck, params=params, J_start=e0["J"], J_best=cur["J"], rel_gain=out["rel_gain"],
           best_eval=cur["eval_id"], stall_confirmed=ready, stall_condition_met=stall_condition, recheck=recheck,
           evidence=report["evidence_for_a_parameter_limit"],
           stall_numerically_limited=out["stall_numerically_limited"], target_met=target, calls=calls)
    return out


def main(argv: list[str]) -> int:
    root, jid = Path(argv[0]), argv[1]
    ws = Workspace(root)
    try:
        result = run_job(ws, jid)
        write_json(ws.root / "jobs" / jid / "result.json", result)
        state = "failed" if result.get("error") else "done"
        _progress(ws, jid, state=state, finished_utc=now_utc())
        append_jsonl(ws.root / "logs" / "jobs.jsonl", {"utc": now_utc(), "job": jid, "event": state})
        return 0
    except Exception as exc:  # noqa: BLE001
        write_json(ws.root / "jobs" / jid / "result.json", {"error": f"{type(exc).__name__}: {exc}",
                                                            "traceback": traceback.format_exc()[-4000:]})
        _progress(ws, jid, state="failed", finished_utc=now_utc(), error=f"{type(exc).__name__}: {exc}")
        append_jsonl(ws.root / "logs" / "jobs.jsonl", {"utc": now_utc(), "job": jid, "event": "failed", "error": str(exc)})
        return 1
