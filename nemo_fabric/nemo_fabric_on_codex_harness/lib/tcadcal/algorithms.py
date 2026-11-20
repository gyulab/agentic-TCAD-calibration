"""
Both work in scaled coordinates x in [0, 1] inside the approved bounds (log10 for densities).
The residual is the stacked J residual R (sum R^2 = J), so the Gauss-Newton model is
consistent with the scalar objective.
"""
from __future__ import annotations

import itertools
import logging
import math
from typing import Any, Callable, Mapping, Sequence

import numpy as np
from scipy.optimize import lsq_linear

from . import config as C
from .state import Workspace, from_unit, to_unit


def bounded_gn_step(R0: np.ndarray, S: np.ndarray, x0: np.ndarray, delta: float | None) -> tuple[np.ndarray, float]:
    """argmin ||R0 + S s||^2 s.t. 0 <= x0 + s <= 1 and |s_i| <= delta (box trust region)."""
    lo = -x0.copy()
    hi = 1.0 - x0
    if delta is not None:
        lo, hi = np.maximum(lo, -delta), np.minimum(hi, delta)
    hi = np.maximum(hi, lo + 1e-15)
    res = lsq_linear(S, -R0, bounds=(lo, hi), method="bvls")
    s = res.x
    return s, float(np.sum((R0 + S @ s) ** 2))


def ofat_rank(R0: np.ndarray, cols: Mapping[str, np.ndarray], x0: Mapping[str, float], delta: float,
              max_group: int = 3) -> list[dict[str, Any]]:
    names = [n for n in cols if np.all(np.isfinite(cols[n]))]
    J0 = float(np.sum(R0 ** 2))
    out = []
    for k in range(1, min(max_group, len(names)) + 1):
        for grp in itertools.combinations(names, k):
            S = np.column_stack([cols[n] for n in grp])
            xg = np.array([x0[n] for n in grp])
            s1, j1 = bounded_gn_step(R0, S, xg, delta)
            sb, jb = bounded_gn_step(R0, S, xg, None)
            out.append({"group": list(grp), "J_now": J0, "J_pred_one_step": j1, "pred_gain_one_step": (J0 - j1) / J0,
                        "step_unit": dict(zip(grp, np.round(s1, 4))), "J_pred_within_bounds": jb,
                        "pred_gain_within_bounds": (J0 - jb) / J0})
    out.sort(key=lambda d: d["J_pred_within_bounds"])
    return out


# ---------------------------------------------------------------------------
# Probe geometry
# ---------------------------------------------------------------------------
def probe_point(x0: float, h: float) -> float:
    """Forward step +h, or -h when +h would leave the box (DFO-LS uses the same rule)."""
    return x0 + h if x0 + h <= 1.0 else x0 - h


def unit_values(ws: Workspace, deck: str, values: Mapping[str, float]) -> dict[str, float]:
    spec = ws.params_for(deck)
    return {n: to_unit(spec[n], values[n]) for n in spec if spec[n].get("lo") is not None}


# ---------------------------------------------------------------------------
# DFO-LS on the selected parameters
# ---------------------------------------------------------------------------
def run_dfols(evaluate: Callable[[dict[str, float], str], dict[str, Any]], spec: Mapping[str, Mapping[str, Any]],
              base_values: Mapping[str, float], group: Sequence[str], base_eval: Mapping[str, Any],
              seeds: Sequence[Mapping[str, Any]], *, rhobeg: float, rhoend: float, maxfun: int,
              log: Callable[[str], None] = print) -> dict[str, Any]:
    """Minimise J over ``group`` with the other parameters fixed. ``evaluate`` returns an eval record."""
    import dfols
    from dfols.evaluations_database import EvaluationDatabase

    logging.getLogger("dfols").setLevel(logging.WARNING)
    group = list(group)
    gspec = {n: spec[n] for n in group}
    x0 = np.array([to_unit(gspec[n], base_values[n]) for n in group])
    R_base = np.asarray(base_eval["residual_vector"], float)
    J_base = float(base_eval["J"])
    penalty = math.sqrt(10.0 * max(J_base, 1.0) / len(R_base))
    trace: list[dict[str, Any]] = []

    def values_at(x: np.ndarray) -> dict[str, float]:
        v = dict(base_values)
        for n, xi in zip(group, x):
            v[n] = from_unit(gspec[n], float(xi))
        return v

    def objfun(x: np.ndarray) -> np.ndarray:
        ev = evaluate(values_at(x), f"dfols {','.join(group)}")
        rec = {"x": [float(t) for t in x], "eval": ev["eval_id"], "J": ev.get("J"), "status": ev["status"],
               "cache_hit": bool(ev.get("cache_hit"))}
        trace.append(rec)
        log(f"  dfols eval {ev['eval_id']} J={ev.get('J')} status={ev['status']} cache={rec['cache_hit']}")
        if ev["status"] == "fatal":
            raise RuntimeError(f"fatal evaluation {ev['eval_id']}: {ev.get('fatal')}")
        if ev["status"] != "ok":
            return np.full(len(R_base), penalty)
        return np.asarray(ev["residual_vector"], float)

    db = EvaluationDatabase()
    db.append(x0.copy(), R_base, make_starting_eval=True)
    used_seeds = []
    for s in seeds:
        xs = np.array([to_unit(gspec[n], s["values"][n]) for n in group])
        if s.get("status") == "ok" and np.max(np.abs(xs - x0)) > 1e-9:
            db.append(xs, np.asarray(s["residual_vector"], float))
            used_seeds.append(s["eval_id"])
    solve_kw = dict(bounds=(np.zeros(len(group)), np.ones(len(group))), npt=len(group) + 1, rhobeg=rhobeg,
                    rhoend=rhoend, maxfun=maxfun,
                    user_params={"init.random_initial_directions": False, "restarts.use_restarts": False},
                    do_logging=False)
    at_upper = bool(np.any(x0 >= 1.0 - 1e-9))
    start_retry = None
    try:
        soln = dfols.solve(objfun, db, **solve_kw)
        failed = at_upper and soln.flag == soln.EXIT_LINALG_ERROR
        why = str(soln.msg)
    except (ZeroDivisionError, np.linalg.LinAlgError, FloatingPointError) as e:
        if not at_upper:
            raise
        failed, why = True, f"{type(e).__name__}: {e}"
    if failed:
        n_first = len(trace)
        log(f"  dfols start at an upper bound failed ({why}); repeated once from x0 without the start database")

        def objfun_x0(x: np.ndarray) -> np.ndarray:
            if np.max(np.abs(np.asarray(x, float) - x0)) <= 1e-9:
                trace.append({"x": [float(t) for t in x], "eval": base_eval["eval_id"], "J": J_base, "status": "ok",
                              "cache_hit": True, "start_reused": True})
                return R_base.copy()
            return objfun(x)

        soln = dfols.solve(objfun_x0, x0.copy(), **solve_kw)
        used_seeds = []
        start_retry = {"reason": why, "evals_before_retry": n_first}
    ok = [t for t in trace if t["status"] == "ok" and t["J"] is not None]
    best = min(ok, key=lambda t: t["J"]) if ok else None
    J_best = best["J"] if best and best["J"] < J_base else J_base
    return {"group": group, "x0_unit": x0.tolist(), "J_base": J_base, "base_eval": base_eval["eval_id"],
            "seed_evals": used_seeds, "best_eval": best["eval"] if best and best["J"] < J_base else None,
            "J_best": J_best, "rel_gain": (J_base - J_best) / J_base, "dJ": J_base - J_best,
            "acceptable": bool(best and J_base - best["J"] >= C.ACCEPT_MIN_DJ),
            "n_evals": len(trace), "n_new_runs_evals": sum(1 for t in trace if not t["cache_hit"]),
            "dfols_flag": int(soln.flag), "dfols_msg": str(soln.msg), "x_solver_unit": [float(t) for t in soln.x],
            "rhobeg": rhobeg, "rhoend": rhoend, "maxfun": maxfun, "trace": trace,
            **({"start_retry": start_retry} if start_retry else {})}


def default_maxfun(n: int) -> int:
    return n + 1 + C.DFOLS_MAXFUN_PER_PARAM * n + C.DFOLS_MAXFUN_BASE
