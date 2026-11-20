#!/usr/bin/env python3
"""Steps after the agent run: model selection, numerical checks and transfer tests without retuning.

    env/bin/python postrun.py select   --run-id RUN-01
    env/bin/python postrun.py check    --run-id RUN-01 [--timeout 3600]
    env/bin/python postrun.py transfer --run-id RUN-01 [--cap 20] [--timeout S] [--card selected|frozen]

select    The fixed selection rule over all evaluations of the run (objective J of the manuscript, J_objective). Candidates are complete,
          converged and numerically valid evaluations in which every added model is active, made by the numerical
          tools or the accepted start evaluation. If any candidate meets the target (every term within tolerance),
          the lowest J is selected, ties within 1e-3 by the earlier evaluation. Otherwise the fewest terms outside
          tolerance, then the smallest summed excess sum(max(0, |e| - 1)), then J, then the earlier evaluation.
          Writes runs/<id>/final/selection.json.
check     Two numerical checks of the selected model, in a budget of their own (at most 4 Sentaurus runs): the model
          on the halved mesh with the stored gate offset kept, and a rerun with the gate offset written into the deck
          (nominal mesh, no alignment). Each passes when every quantity changes by less than 1/10 of its tolerance
          (config.VERIFY_CRITERIA). Writes runs/<id>/final/verification.json.
transfer  Tests without retuning: the model at every bias and device of the run's test sets (config.SPLITS, or
          SPLITS_RECAL for a recalibration), with the same parameter values and no gate alignment. V_th is read at
          10 nA x W/L of each device. Writes runs/<id>/final/transfer_tests.json and .csv.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from tcadcal import config as C  # noqa: E402
from tcadcal import deck as D  # noqa: E402
from tcadcal import evaluation as E  # noqa: E402
from tcadcal import measurements as M  # noqa: E402
from tcadcal import objective as O  # noqa: E402
from tcadcal import sentaurus as S  # noqa: E402
from tcadcal import verify as V  # noqa: E402
from tcadcal.state import ACCEPT_SOURCES, Workspace, job_kind  # noqa: E402
from tcadcal.util import now_utc, read_json, read_jsonl, write_json  # noqa: E402

TIE = 1e-3
CHECK_CAP = 4
METRIC_KEYS = ("Vth_sim_V", "SS_sim_mV_dec", "dlogIon_dec", "E_lin")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def candidates(ws: Workspace) -> list[dict[str, Any]]:
    """Eligible evaluations of the run."""
    baseline = next((h.get("eval") for h in read_jsonl(ws.state / "history.jsonl")
                     if h.get("kind") == "accept" and h.get("J_before") is None), None)
    out = []
    for f in sorted((ws.root / "evals").glob("E*.json")):
        e = read_json(f)
        if not e or e.get("status") != "ok" or not e.get("complete") or not e.get("converged"):
            continue
        if ws.alignment_loss in C.GATE_FREE_LOSSES and not (e.get("alignment") or {}).get("defined"):
            continue
        if e.get("validity") not in (None, "VALID"):
            continue
        if any(a.get("label") == "INACTIVE" for a in (e.get("activation") or {}).values()):
            continue
        if not e.get("J_terms"):
            continue
        if job_kind(ws, e.get("job")) not in ACCEPT_SOURCES and e["eval_id"] != baseline:
            continue
        out.append(e)
    return out


def acceptance_view(e: Mapping[str, Any]) -> dict[str, Any]:
    """The terms of J in tolerance units: e = sqrt(term), excess = sum of max(0, e - 1)."""
    terms = e["J_terms"]
    acc = O.acceptance_terms("J_objective")
    missing = [t for t in acc if t not in terms]
    if missing:
        raise ValueError(f"{e.get('eval_id')}: J terms missing: {missing}")
    viol = [t for t in acc if terms[t] > 1.0]
    return {"acceptance_met": not viol, "violations": viol,
            "violation_excess_sum": float(sum(max(0.0, float(np.sqrt(terms[t])) - 1.0) for t in acc)),
            "J_primary": float(e["J"])}


def select(ws: Workspace) -> dict[str, Any]:
    if ws.objective != "J_objective":
        raise SystemExit(f"the selection rule is defined for objective J_objective, not {ws.objective}")
    cands = candidates(ws)
    if not cands:
        return {"selected": None, "reason": "no eligible evaluation"}
    views = {e["eval_id"]: acceptance_view(e) for e in cands}
    met = [e for e in cands if views[e["eval_id"]]["acceptance_met"]]
    if met:
        best = min(e["J"] for e in met)
        sel = min((e for e in met if e["J"] <= best + TIE), key=lambda e: e["eval_id"])
        rule = "target met: lowest J (ties within 1e-3 by the earlier evaluation)"
    else:
        sel = min(cands, key=lambda e: (len(views[e["eval_id"]]["violations"]), views[e["eval_id"]]["violation_excess_sum"],
                                        e["J"], e["eval_id"]))
        rule = f"target not met: fewest of the {len(O.acceptance_terms('J_objective'))} terms outside tolerance, then summed excess, then J"
    frozen = read_json(ws.state / "FROZEN") or {}
    acc = (frozen.get("accepted") or {}).get("eval")
    return {"utc": now_utc(), "rule": rule, "objective": "J_objective", "n_candidates": len(cands),
            "n_meeting_target": len(met), "selected": sel["eval_id"], "selected_J": sel["J"],
            "selected_view": views[sel["eval_id"]], "selected_terms": sel["J_terms"],
            "selected_values": sel["values"], "selected_values_rendered": sel.get("values_rendered"),
            "selected_alignment": sel.get("alignment"), "agent_frozen_card": acc,
            "agent_card_is_selected": acc == sel["eval_id"] if acc else None,
            "agent_card_view": (acceptance_view(E.load_eval(ws, acc)) if acc else None)}


# ---------------------------------------------------------------------------
# Numerical checks
# ---------------------------------------------------------------------------
def _curves(runs) -> dict[float, tuple[np.ndarray, np.ndarray]]:
    return {float(r["vd"]): S.load_curve(r) for r in runs}


def _shifted(curves: Mapping[float, tuple[np.ndarray, np.ndarray]], shift: float) -> list[tuple[np.ndarray, np.ndarray]]:
    return [(vg + shift, i) for _, (vg, i) in sorted(curves.items())]


def assess(bench: Mapping[float, tuple[np.ndarray, np.ndarray]], fine: Mapping[float, tuple[np.ndarray, np.ndarray]],
           preps: Sequence[O.MeasPrep], shift: float, objective: str = "J_objective") -> dict[str, Any]:
    """The model on the nominal and on the halved mesh: J, target, metrics, their change and the mesh criteria."""
    preps = sorted(preps, key=lambda p: p.vd)
    sb, sf = O.score(preps, _shifted(bench, shift), objective), O.score(preps, _shifted(fine, shift), objective)
    ss_meas = {float(p.vd): 1e3 * O.extract(p.vg, np.abs(p.i_raw), p.ss_low_a)["ss_V_dec"] for p in preps}
    acc = O.acceptance_terms(objective)
    cmp = V.compare_curves(fine, bench, ss_meas, preps=preps, acceptance=acc)   # halved vs nominal mesh, no alignment
    metrics = {}
    for k, b in sb["branches"].items():
        f = sf["branches"][k]
        metrics[k] = {m: {"nominal": b.get(m), "halved": f.get(m),
                          "change": None if b.get(m) is None or f.get(m) is None else f[m] - b[m]} for m in METRIC_KEYS}
    out = {"objective": objective, "J_nominal": sb["J"], "J_halved": sf["J"], "J_change": sf["J"] - sb["J"],
           "target_nominal": bool(sb["target"]), "target_halved": bool(sf["target"]),
           "terms_nominal": sb["terms"], "terms_halved": sf["terms"], "metrics": metrics,
           "mesh_criteria_pass": bool(cmp["pass"]), "mesh_criteria_fail": cmp["fail"],
           "change_x3_indicator": cmp["change_x3_indicator"], "ss_profile_change": cmp.get("ss_profile_change"),
           "acceptance_set_criteria": cmp.get("acceptance_set")}
    if out["acceptance_set_criteria"] is not None:
        out["verified_success_acceptance_set"] = out["target_nominal"] and out["acceptance_set_criteria"]["pass"]
    out["verified_success"] = out["target_nominal"] and out["mesh_criteria_pass"]
    return out


def check(ws: Workspace, root: Path, timeout: int) -> dict[str, Any]:
    sel = read_json(root / "selection.json")
    if not sel or not sel.get("selected"):
        raise SystemExit("run `postrun.py select` first")
    e = E.load_eval(ws, sel["selected"])
    templates, spec = D.read_templates(ws.deck_dir(e["deck"])), ws.params_for(e["deck"])
    store = root / "store"
    if not (store / "support_md5.json").exists():
        S.init_store(store)
    budget = S.Budget(root / "budget.json", CHECK_CAP)
    preps = [O.prepare(M.load_forward(dev, vd), C.ss_low(ws.program)) for dev, vd in ws.calibration]
    shift = float(e["alignment"]["shift_V"])
    bench = _curves(e["runs"])
    out: dict[str, Any] = {"utc_start": now_utc(), "eval": e["eval_id"], "objective": ws.objective, "shift_V": shift}
    # 1. halved mesh, rendered values, the stored gate offset kept
    fine, runs = {}, []
    for dev, vd in ws.calibration:
        r = S.run_one(store, templates, spec, e["values_rendered"], dev, vd, budget, mesh_scale=0.5,
                      levels=list(C.CALIBRATION_RECOVERY_LEVELS), violations_log=root / "violations.jsonl",
                      timeout_s=timeout, budget_category="final_verification")
        runs.append({k: r.get(k) for k in ("vd", "ok", "reason", "recovery_name", "run_key", "validity")})
        if r.get("ok"):
            fine[float(vd)] = S.load_curve(r)
    out["halved_mesh"] = {"runs": runs}
    if len(fine) == len(bench):
        out["halved_mesh"].update(assess(bench, fine, preps, shift, ws.objective))
    else:
        out["halved_mesh"]["error"] = "the halved mesh did not converge (recorded as a failed check)"
    # 2. the gate offset written into the deck, nominal mesh, no alignment. Not applicable when the gate offset is
    # fixed and no shift was applied: the stored curves are then already those of the actual deck.
    if "WF_gate" in ws.fixed and not (e.get("alignment") or {}).get("defined"):
        out["aligned_wf_rerun"] = {"not_applicable": "the gate offset is fixed in this run and no shift was applied; the "
                                                     "stored curves are the actual deck"}
        out["budget"] = budget.used()
        out["summary"] = summary(out)
        out["utc_end"] = now_utc()
        return out
    aligned_vals = dict(e["values"])
    rer, runs2 = {}, []
    for dev, vd in ws.calibration:
        r = S.run_one(store, templates, spec, aligned_vals, dev, vd, budget, levels=list(C.CALIBRATION_RECOVERY_LEVELS),
                      violations_log=root / "violations.jsonl", timeout_s=ws.meta.get("sdevice_timeout_s"),
                      budget_category="final_verification")
        runs2.append({k: r.get(k) for k in ("vd", "ok", "reason", "recovery_name", "run_key", "validity")})
        if r.get("ok"):
            rer[float(vd)] = S.load_curve(r)
    out["aligned_wf_rerun"] = {"WF_gate_in_deck_eV": aligned_vals["WF_gate"], "runs": runs2}
    if len(rer) == len(bench):
        shifted = {vd: (vg + shift, i) for vd, (vg, i) in bench.items()}
        ss_meas = {float(p.vd): 1e3 * O.extract(p.vg, np.abs(p.i_raw), p.ss_low_a)["ss_V_dec"] for p in preps}
        cmp = V.compare_curves(rer, shifted, ss_meas, preps=sorted(preps, key=lambda p: p.vd),
                               acceptance=O.acceptance_terms(ws.objective))
        sc = O.score(sorted(preps, key=lambda p: p.vd), [rer[vd] for vd in sorted(rer)], ws.objective)
        out["aligned_wf_rerun"].update({"criteria_pass": bool(cmp["pass"]), "criteria_fail": cmp["fail"],
                                        "per_vd": cmp["per_vd"], "ss_profile_change": cmp.get("ss_profile_change"),
                                        "acceptance_set_criteria": cmp.get("acceptance_set"),
                                        "J_rerun": sc["J"], "target_rerun": bool(sc["target"]),
                                        "J_stored": e["J"], "terms_rerun": sc["terms"]})
    else:
        out["aligned_wf_rerun"]["error"] = "the rerun with the gate offset in the deck did not converge"
    out["budget"] = budget.used()
    out["summary"] = summary(out)
    out["utc_end"] = now_utc()
    return out


def summary(out: Mapping[str, Any]) -> dict[str, Any]:
    """The flags of the two checks in one place. The numerical verdict judges the terms of J."""
    hm, ar = out.get("halved_mesh") or {}, out.get("aligned_wf_rerun") or {}
    ha = hm.get("acceptance_set_criteria")
    if ar.get("not_applicable"):
        return {"halved_mesh_converged": "error" not in hm, "halved_mesh_criteria_pass": hm.get("mesh_criteria_pass"),
                "aligned_wf_rerun": "not applicable (gate offset fixed, no shift)",
                "target_nominal": hm.get("target_nominal"), "target_halved": hm.get("target_halved"),
                "acceptance_set_halved_mesh_pass": None if ha is None else ha["pass"],
                "numerical_verdict_acceptance_set": None if ha is None else bool(ha["pass"] and "error" not in hm)}
    s = {"halved_mesh_converged": "error" not in hm, "halved_mesh_criteria_pass": hm.get("mesh_criteria_pass"),
         "aligned_wf_rerun_converged": "error" not in ar, "aligned_wf_rerun_criteria_pass": ar.get("criteria_pass"),
         "target_nominal": hm.get("target_nominal"), "target_halved": hm.get("target_halved"),
         "target_rerun": ar.get("target_rerun"), "verified_success": hm.get("verified_success")}
    s["both_numerical_checks_pass"] = bool(s["halved_mesh_criteria_pass"] and s["aligned_wf_rerun_criteria_pass"])
    ra = ar.get("acceptance_set_criteria")
    s["acceptance_set_halved_mesh_pass"] = None if ha is None else ha["pass"]
    s["acceptance_set_aligned_wf_rerun_pass"] = None if ra is None else ra["pass"]
    s["numerical_verdict_acceptance_set"] = (None if ha is None or ra is None
                                             else bool(ha["pass"] and ra["pass"] and s["halved_mesh_converged"]
                                                       and s["aligned_wf_rerun_converged"]))
    return s


# ---------------------------------------------------------------------------
# Transfer tests without retuning
# ---------------------------------------------------------------------------
def summarize(curves: list[dict]) -> dict[str, dict]:
    """Per test set: mean and largest |dV_th| and |dlog10 I_on|."""
    out = {}
    for split in dict.fromkeys(c["split"] for c in curves):
        ok = [c for c in curves if c["split"] == split and c.get("ok")]
        vth = [abs(c["dVth_V"]) * 1e3 for c in ok if c.get("dVth_V") is not None]
        ion = [abs(c["dlogIon_dec"]) for c in ok if c.get("dlogIon_dec") is not None]
        out[split] = {"n": len(ok), "n_failed": sum(1 for c in curves if c["split"] == split and not c.get("ok")),
                      "vth_mean_mV": float(np.mean(vth)) if vth else None, "vth_max_mV": float(np.max(vth)) if vth else None,
                      "n_vth_undefined": len(ok) - len(vth),
                      "ion_mean_dec": float(np.mean(ion)) if ion else None, "ion_max_dec": float(np.max(ion)) if ion else None,
                      "n_all_within_tolerance": sum(1 for c in ok if c.get("all_metrics_within_tolerance"))}
    return out


def transfer(ws: Workspace, root: Path, out_dir: Path, run_id: str, card: str, cap: int, timeout: int | None) -> Path:
    frozen = read_json(ws.state / "FROZEN")
    if card == "frozen":
        if not frozen:
            raise SystemExit("the run is not frozen (approved final proposal and `state freeze` required)")
        acc = frozen["accepted"]
    else:
        sel = read_json(out_dir / "selection.json") or {}
        if not sel.get("selected"):
            raise SystemExit("no selected model for this run: run `postrun.py select` first")
        ev = read_json(ws.root / "evals" / f"{sel['selected']}.json")
        acc = {"deck": ev["deck"], "values": ev["values"], "eval": ev["eval_id"], "source": "selection rule"}
    deck_v, values = acc["deck"], acc["values"]
    templates = D.read_templates(ws.deck_dir(deck_v))
    spec = ws.params_for(deck_v)
    store = out_dir / "transfer_store"
    if not (store / "support_md5.json").exists():
        S.init_store(store)
    budget = S.Budget(out_dir / "transfer_budget.json", cap)
    conds = [(split, dev, vd) for split, items in C.splits(ws.program).items() for dev, vd in items]
    tmo = timeout or ws.meta.get("sdevice_timeout_s")
    runs = S.simulate(store, templates, spec, values, [(d, v) for _, d, v in conds], budget,
                      violations_log=out_dir / "transfer_violations.jsonl", timeout_s=tmo)
    rows, out = [], {"run_id": run_id, "deck": deck_v, "values": values, "eval": acc["eval"], "card": card,
                     "utc": now_utc(), "objective": ws.objective, "timeout_s": tmo, "curves": []}
    for (split, dev, vd), r in zip(conds, runs):
        rec = {"split": split, "device": dev, "vd": vd, "ok": r.get("ok"), "reason": r.get("reason"),
               "mesh_readback": r.get("mesh_readback"), "run_key": r.get("run_key")}
        if r.get("ok"):
            # V_th at 10 nA x W/L of this device, set for this curve only and restored afterwards
            lvl0 = C.VTH_LEVEL_A
            C.VTH_LEVEL_A = C.vth_level_A(dev)
            try:
                prep = O.prepare(M.load_forward(dev, vd), C.ss_low(ws.program))
                sc = O.score([prep], [S.load_curve(r)], objective=ws.objective)   # same J definition, no alignment
            finally:
                C.VTH_LEVEL_A = lvl0
            rec["vth_level_A"] = C.vth_level_A(dev)
            b = next(iter(sc["branches"].values()))
            rec.update({"J": sc["J"],
                        **{k: b[k] for k in ("dVth_V", "dlogIon_dec", "E_lin", "SS_sim_mV_dec", "SS_meas_mV_dec",
                                             "Vth_sim_V", "Vth_meas_V")},
                        "metric_errors": b["metric_errors"], "all_metrics_within_tolerance": b["target"],
                        "regions": b["regions"],
                        "curve": {k: np.asarray(b["curve"][k], float).tolist() for k in ("vg", "i_meas", "i_sim", "r")}})
        out["curves"].append(rec)
        rows.append({k: rec.get(k) for k in ("split", "device", "vd", "ok", "J", "dVth_V", "dlogIon_dec", "E_lin",
                                             "SS_sim_mV_dec", "SS_meas_mV_dec", "all_metrics_within_tolerance")})
    out["summary_by_split"] = summarize(out["curves"])
    write_json(out_dir / "transfer_tests.json", out)
    with open(out_dir / "transfer_tests.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return out_dir / "transfer_tests.csv"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["select", "check", "transfer"])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--timeout", type=int, default=None,
                    help="per-run limit in s (check: default config.SDEVICE_TIMEOUT_S, since the halved mesh runs "
                         "several times longer; transfer: default the run's own limit)")
    ap.add_argument("--cap", type=int, default=20, help="transfer: Sentaurus runs allowed for the transfer tests")
    ap.add_argument("--card", choices=["selected", "frozen"], default="selected",
                    help="transfer: the model chosen by the selection rule (default) or the agent's frozen final model")
    a = ap.parse_args()
    root = C.RUNS_ROOT / a.run_id
    ws = Workspace(root / "workspace")
    out_dir = root / "final"
    out_dir.mkdir(exist_ok=True)
    if a.cmd == "select":
        res = select(ws)
        write_json(out_dir / "selection.json", res)
        print({k: v for k, v in res.items() if k not in ("selected_view", "selected_terms", "agent_card_view")})
    elif a.cmd == "check":
        res = check(ws, out_dir, a.timeout or C.SDEVICE_TIMEOUT_S)
        write_json(out_dir / "verification.json", res)
        print(res["summary"])
    else:
        print(transfer(ws, root, out_dir, a.run_id, a.card, a.cap, a.timeout))


if __name__ == "__main__":
    main()
