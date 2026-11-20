"""Objective J, device metrics, calibration target, gate work-function alignment, residual diagnostics.

Transfer-curve error (per calibration curve b, averaged over both curves):
    g_X,i   = log10(max(|I_X,i|, I_LOQ,b))            X = TCAD or measured, both clipped at the
                                                     detection limit I_LOQ,b = 10 sigma_I,b
    S_X,i   = |d g_X / d V_G|                          central differences on the 50 mV grid
    sigma_i^2 = (sigma_V * max(S_meas,i, S_TCAD,i))^2 + sigma_r^2
                + (sigma_I,b / (ln10 * max(|I_meas,i|, I_LOQ,b)))^2
    r_i     = (g_TCAD,i - g_meas,i) / sigma_i,   error_b = mean_i r_i^2
sigma_I,b = 1.4826 x MAD of the forward readings at V_G <= -0.75 V.

Objective J: the mean of 14 terms in tolerance units, seven per calibration curve: V_th at 10 nA x W/L (50 mV), the SS
profile (15 % of the measured SS), log10 I_on (0.05 dec), E_lin (0.10) and the curve terms of three regions
(subthreshold, turn-on, on). Calibration target: every term within tolerance (term <= 1).

Gate work-function alignment: the gate work function only shifts the transfer curve along V_G,
so the shift d that minimises J is solved after each simulation (``align``) on the simulated sweep,
which extends ALIGN_WINDOW_V beyond the measured range.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np

from . import config as C
from .measurements import ForwardCurve, vd_tag

LN10 = math.log(10.0)


# ---------------------------------------------------------------------------
# Measured-curve preparation (frozen per curve)
# ---------------------------------------------------------------------------
@dataclass
class MeasPrep:
    device: str
    vd: float
    vg: np.ndarray
    i_raw: np.ndarray
    sigma_I: float
    loq: float
    g: np.ndarray
    slope: np.ndarray
    censored: np.ndarray
    region: np.ndarray            # "off", "subthreshold", "turn_on", "on"
    ion: float
    extra: dict = field(default_factory=dict)
    ss_low_a: float = C.SS_LOW_A

    @property
    def i_abs(self) -> np.ndarray:
        return np.abs(self.i_raw)


def robust_sigma(values: np.ndarray) -> float:
    med = float(np.median(values))
    return C.MAD_TO_SIGMA * float(np.median(np.abs(values - med)))


def log_slope(vg: np.ndarray, g: np.ndarray) -> np.ndarray:
    return np.abs(np.gradient(g, vg))


def prepare(curve: ForwardCurve, ss_low_a: float = C.SS_LOW_A) -> MeasPrep:
    vg, i_raw = curve.vg, curve.i_raw
    win = vg <= C.noise_vg_max(curve.device) + C.VG_TOL_V
    sigma_i = robust_sigma(i_raw[win])
    if not sigma_i > 0:
        raise ValueError(f"non-positive noise scale for {curve.device} VD={curve.vd}")
    loq = C.LOQ_K * sigma_i
    i_abs = np.abs(i_raw)
    g = np.log10(np.maximum(i_abs, loq))
    slope = log_slope(vg, g)
    censored = i_abs < loq
    ion = float(np.max(i_abs))
    region = np.where(censored, "off",
                      np.where(i_abs < C.VTH_LEVEL_A, "subthreshold", np.where(i_abs < ion / 10.0, "turn_on", "on")))
    wins, src = ss_windows(i_abs, loq), "computed"
    frozen = frozen_ss_windows(curve.device, curve.vd)
    if frozen is not None and _source_sha(curve.source) == frozen_ss_source(curve.device, curve.vd):
        same = len(frozen) == len(wins) and all(abs(a["lo_A"] - b["lo_A"]) <= 1e-6 * b["lo_A"] and
                                                abs(a["hi_A"] - b["hi_A"]) <= 1e-6 * b["hi_A"] for a, b in zip(wins, frozen))
        if not same:
            raise ValueError(f"SS windows of {curve.device} V_D = {curve.vd:g} V differ from the frozen windows "
                             f"({C.SS_WINDOWS_FROZEN.name}): the measured curve or its preparation changed")
        wins, src = [dict(w) for w in frozen], "frozen"
    return MeasPrep(device=curve.device, vd=curve.vd, vg=vg, i_raw=i_raw, sigma_I=sigma_i, loq=loq, g=g,
                    slope=slope, censored=censored, region=region, ion=ion,
                    extra={"n_censored": int(censored.sum()), "noise_window_n": int(win.sum()), "ss_windows": wins,
                           "ss_windows_source": src}, ss_low_a=ss_low_a)


# ---------------------------------------------------------------------------
# Simulated curve on the measured grid
# ---------------------------------------------------------------------------
def sim_on_grid(sim_vg: Sequence[float], sim_i: Sequence[float], vg: np.ndarray) -> np.ndarray:
    from scipy.interpolate import CubicSpline
    sv = np.asarray(sim_vg, float)
    si = np.maximum(np.abs(np.asarray(sim_i, float)), C.CURRENT_FLOOR_A)
    order = np.argsort(sv)
    sv, si = sv[order], si[order]
    keep = np.concatenate([[True], np.diff(sv) > 1e-9])
    sv, si = sv[keep], si[keep]
    if vg.min() < sv.min() - C.VG_TOL_V or vg.max() > sv.max() + C.VG_TOL_V:
        raise ValueError(f"simulated sweep [{sv.min():.4f}, {sv.max():.4f}] V does not cover "
                         f"[{vg.min():.4f}, {vg.max():.4f}] V")
    lg = np.log10(si)
    if len(sv) < 4:
        return 10 ** np.interp(vg, sv, lg)
    out = CubicSpline(sv, lg)(np.clip(vg, sv.min(), sv.max()))
    return 10 ** np.clip(out, lg.min(), lg.max())


# ---------------------------------------------------------------------------
# J residuals
# ---------------------------------------------------------------------------
def branch_residuals(prep: MeasPrep, sim_vg: Sequence[float], sim_i: Sequence[float]) -> dict[str, Any]:
    i_s = sim_on_grid(sim_vg, sim_i, prep.vg)
    g_s = np.log10(np.maximum(i_s, prep.loq))
    s_s = log_slope(prep.vg, g_s)
    s_eff = np.maximum(prep.slope, s_s)
    noise = prep.sigma_I / (LN10 * np.maximum(prep.i_abs, prep.loq))
    sigma = np.sqrt((C.SIGMA_V * s_eff) ** 2 + C.SIGMA_R ** 2 + noise ** 2)
    dg = g_s - prep.g
    r = dg / sigma
    return {"i_sim": i_s, "g_sim": g_s, "dg": dg, "sigma": sigma, "r": r, "J_branch": float(np.mean(r ** 2))}


def _crossing_from_right(vg: np.ndarray, ids: np.ndarray, level: float) -> float | None:
    order = np.argsort(vg)
    vg, ids = vg[order], np.maximum(ids[order], C.CURRENT_FLOOR_A)
    env = np.minimum.accumulate(ids[::-1])[::-1]
    if env[-1] < level or env[0] >= level:
        return None
    k = int(np.where(env < level)[0][-1])
    x0, x1 = vg[k], vg[k + 1]
    y0, y1 = math.log10(env[k]), math.log10(env[k + 1])
    if y1 == y0:
        return float(x1)
    return float(x0 + (math.log10(level) - y0) * (x1 - x0) / (y1 - y0))


def extract(vg: np.ndarray, ids: np.ndarray, ss_low_a: float = C.SS_LOW_A) -> dict[str, Any]:
    vth = _crossing_from_right(vg, ids, C.VTH_LEVEL_A)
    v_lo = _crossing_from_right(vg, ids, ss_low_a)
    ss = None
    if vth is not None and v_lo is not None:
        ss = (vth - v_lo) / (math.log10(C.VTH_LEVEL_A) - math.log10(ss_low_a))
    return {"vth_100nA_V": vth, "ss_V_dec": ss, "ion_A": float(np.max(ids)), "ss_low_A": ss_low_a}


def ss_windows(i_abs: np.ndarray, loq: float, top: float | None = None) -> list[dict[str, Any]]:
    """One-decade windows of one measured curve: in half-decade steps from its quantification limit (LOQ = 10 x robust
    noise) up to the V_th level, plus one window ending at the V_th level. A window with fewer than min_points measured
    points between its edges is widened upward in half-decade steps, at most to the V_th level, and its SS uses the
    actual span. A window that still has too few points is dropped."""
    p = C.SS_PROFILE
    top = C.VTH_LEVEL_A if top is None else top
    i_abs = np.abs(np.asarray(i_abs, float))
    wdec, sdec = p["window_dec"], p["step_dec"]

    def npts(lo: float, hi: float) -> int:
        return int(np.sum((i_abs >= lo) & (i_abs <= hi)))
    lows, k = [], 0
    while loq * 10 ** (k * sdec + wdec) <= top * (1 + 1e-9):
        lows.append(loq * 10 ** (k * sdec))
        k += 1
    top_lo = top / 10 ** wdec
    if top_lo >= loq and (not lows or lows[-1] * 10 ** wdec < top * (1 - 1e-6)):
        lows.append(top_lo)
    out = []
    for lo in lows:
        hi = min(top, lo * 10 ** wdec)
        while npts(lo, hi) < p["min_points"] and hi < top * (1 - 1e-9):
            hi = min(top, hi * 10 ** sdec)
        n = npts(lo, hi)
        if n < p["min_points"]:
            continue
        out.append({"lo_A": float(f"{lo:.6g}"), "hi_A": float(f"{hi:.6g}"), "span_dec": round(math.log10(hi / lo), 6),
                    "n_points": n, "merged": bool(hi > lo * 10 ** wdec * (1 + 1e-9))})
    return out


def ss_profile(vg: np.ndarray, ids: np.ndarray, windows: Sequence[Mapping[str, Any]]) -> list[float | None]:
    """SS of each window (mV/dec): the V_G span between the crossings of its edges (linear in log10 I on the monotone
    envelope from the right, the V_th extraction) divided by its logarithmic span."""
    vg, ids = np.asarray(vg, float), np.abs(np.asarray(ids, float))
    out: list[float | None] = []
    for w in windows:
        a, b = _crossing_from_right(vg, ids, w["lo_A"]), _crossing_from_right(vg, ids, w["hi_A"])
        out.append(None if a is None or b is None else 1e3 * (b - a) / w["span_dec"])
    return out


_FROZEN: dict[str, Any] = {}


def frozen_ss_windows(device: str, vd: float) -> list[dict[str, Any]] | None:
    if "data" not in _FROZEN:
        import json
        curves: dict[str, Any] = {}
        for p in dict.fromkeys([C.SS_WINDOWS_FROZEN, *C.SS_WINDOWS_FROZEN_BY_PROGRAM.values()]):
            if p.exists():
                for k, v in (json.loads(p.read_text()).get("curves") or {}).items():
                    if k in curves:
                        raise ValueError(f"frozen SS windows: curve {k} in more than one frozen file")
                    curves[k] = v
        _FROZEN["data"] = {"curves": curves}
    return ((_FROZEN["data"].get("curves") or {}).get(f"{device}_vd{vd_tag(vd)}") or {}).get("windows")


def frozen_ss_source(device: str, vd: float) -> str | None:
    frozen_ss_windows(device, vd)
    return ((_FROZEN["data"].get("curves") or {}).get(f"{device}_vd{vd_tag(vd)}") or {}).get("source_sha256")


_SHA: dict[str, str | None] = {}


def _source_sha(source: str | None) -> str | None:
    if not source:
        return None
    if source not in _SHA:
        import hashlib
        from pathlib import Path
        p = Path(source)
        _SHA[source] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return _SHA[source]


def ss_profile_branch(prep: "MeasPrep", i_sim: np.ndarray) -> dict[str, Any]:
    """SS profile of TCAD against the measured curve on the same windows, the same V_G points (TCAD on the measured grid)
    and the same extraction. The term error is the worst window: (SS_TCAD - SS_meas) / (tol x SS_meas)."""
    wins = prep.extra.get("ss_windows") or ss_windows(prep.i_abs, prep.loq)
    tol = C.SS_PROFILE["tol_rel"]
    sm, st = ss_profile(prep.vg, prep.i_abs, wins), ss_profile(prep.vg, i_sim, wins)
    rows = []
    for w, m, t in zip(wins, sm, st):
        e = None if m is None or t is None or m <= 0 else (t - m) / (tol * m)
        rows.append({**w, "SS_meas_mV_dec": m, "SS_TCAD_mV_dec": t, "error_tol": e})
    valid = [r for r in rows if r["SS_meas_mV_dec"] is not None]
    worst = None
    if not valid or any(r["error_tol"] is None for r in valid):
        error = C.MISSING_METRIC_RESIDUAL       # TCAD does not cross a window edge: counts as 3 tolerances
    else:
        worst = max(valid, key=lambda r: abs(r["error_tol"]))
        error = worst["error_tol"]
    mins = lambda key: min((r[key] for r in valid if r[key] is not None), default=None)  # noqa: E731
    return {"windows": rows, "error": float(error),
            "worst_window_A": None if worst is None else [worst["lo_A"], worst["hi_A"]],
            "SS_min_meas_mV_dec": mins("SS_meas_mV_dec"), "SS_min_TCAD_mV_dec": mins("SS_TCAD_mV_dec"),
            "source": prep.extra.get("ss_windows_source", "computed")}


def metric_branch(sim_vg: Sequence[float], sim_i: Sequence[float], meas_vg: np.ndarray, meas_i_raw: np.ndarray,
               ss_low_a: float = C.SS_LOW_A) -> dict[str, Any]:
    sv = np.asarray(sim_vg, float)
    si = np.maximum(np.abs(np.asarray(sim_i, float)), C.CURRENT_FLOOR_A)
    order = np.argsort(sv)
    sv, si = sv[order], si[order]
    mv = np.asarray(meas_vg, float)
    mi = np.maximum(np.abs(np.asarray(meas_i_raw, float)), C.CURRENT_FLOOR_A)
    lo, hi = max(sv.min(), mv.min()), min(sv.max(), mv.max())
    keep = (mv >= lo - 1e-9) & (mv <= hi + 1e-9)
    vg, i_m = mv[keep], mi[keep]
    i_s = 10 ** np.interp(vg, sv, np.log10(si))
    meas_x = extract(vg, i_m, ss_low_a)
    ss_low = ss_low_a
    if meas_x["ss_V_dec"] is None and meas_x["vth_100nA_V"] is not None:
        below = i_m[vg <= meas_x["vth_100nA_V"]]
        ss_low = max(ss_low_a, 1.5 * float(np.min(below)))
        meas_x = extract(vg, i_m, ss_low)
    smask = sv <= C.VG_STOP + 1e-9
    sim_x = extract(sv[smask], si[smask], ss_low)
    mask_log = i_m >= C.LOG_FLOOR_A
    e_log = float(np.sqrt(np.mean((np.log10(i_s[mask_log]) - np.log10(i_m[mask_log])) ** 2)))
    i_max_m = float(np.max(i_m))
    mask_lin = i_m >= i_max_m / 10.0
    e_lin = float(np.sqrt(np.mean(((i_s[mask_lin] - i_m[mask_lin]) / i_max_m) ** 2)))
    k_on = int(np.argmin(np.abs(vg - C.VG_ION_V)))
    ion_vg = float(vg[k_on])
    ion_m, ion_s = float(i_m[k_on]), float(i_s[k_on])
    d_on = math.log10(ion_s / ion_m)
    d_th = None if sim_x["vth_100nA_V"] is None or meas_x["vth_100nA_V"] is None else sim_x["vth_100nA_V"] - meas_x["vth_100nA_V"]
    d_ss = None if sim_x["ss_V_dec"] is None or meas_x["ss_V_dec"] is None else sim_x["ss_V_dec"] - meas_x["ss_V_dec"]
    return {"E_log_dec": e_log, "E_lin": e_lin,
            "dVth_V": d_th, "dlogIon_dec": d_on, "dSS_V_dec": d_ss,
            "Vth_sim_V": sim_x["vth_100nA_V"], "Vth_meas_V": meas_x["vth_100nA_V"],
            "SS_sim_mV_dec": None if sim_x["ss_V_dec"] is None else 1e3 * sim_x["ss_V_dec"],
            "SS_meas_mV_dec": None if meas_x["ss_V_dec"] is None else 1e3 * meas_x["ss_V_dec"],
            "Ion_sim_A": ion_s, "Ion_meas_A": ion_m, "Ion_VG_V": ion_vg, "ss_low_A": ss_low,
            "missing": [k for k, v in (("dVth", d_th), ("dSS", d_ss)) if v is None]}


REGIONS = ("off", "subthreshold", "turn_on", "on")


def runs_test_p(signs: np.ndarray) -> float | None:
    """One-sided Wald-Wolfowitz p-value for too few sign runs (clustered residuals)."""
    s = np.asarray(signs)
    s = s[s != 0]
    n = len(s)
    if n < 10:
        return None
    n1, n2 = int((s > 0).sum()), int((s < 0).sum())
    if n1 == 0 or n2 == 0:
        return 0.0
    runs = 1 + int(np.sum(s[1:] != s[:-1]))
    mu = 2.0 * n1 * n2 / n + 1.0
    var = 2.0 * n1 * n2 * (2.0 * n1 * n2 - n) / (n * n * (n - 1.0))
    if var <= 0:
        return None
    z = (runs - mu) / math.sqrt(var)
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def region_summary(prep: MeasPrep, res: Mapping[str, Any]) -> dict[str, Any]:
    r, dg = np.asarray(res["r"]), np.asarray(res["dg"])
    total = float(np.sum(r ** 2)) or 1.0
    out: dict[str, Any] = {}
    for reg in REGIONS:
        m = prep.region == reg
        if not m.any():
            out[reg] = {"n": 0}
            continue
        same = max(float((dg[m] > 0).mean()), float((dg[m] < 0).mean()))
        out[reg] = {"n": int(m.sum()), "vg_range_V": [float(prep.vg[m].min()), float(prep.vg[m].max())],
                    "mean_dlog_dec": float(np.mean(dg[m])), "max_abs_dlog_dec": float(np.max(np.abs(dg[m]))),
                    "mean_r": float(np.mean(r[m])), "mean_r2": float(np.mean(r[m] ** 2)),
                    "same_sign_fraction": same, "share_of_curve_error": float(np.sum(r[m] ** 2) / total),
                    "sign": "TCAD_high" if np.mean(dg[m]) > 0 else "TCAD_low"}
    quant = prep.region != "off"
    out["runs_test_p"] = runs_test_p(np.sign(dg[quant]))
    return out


METRICS = ("dVth", "dSS", "dlogIon", "E_lin")
METRICS_OBJECTIVE = ("dVth", "dSS_profile", "dlogIon", "E_lin")
# J term names = the step-record item names (state.TARGET_KEYS): 'V_th@0.05', 'V_th_shift', 'region:on@1'
TERM_NAMES = {"dVth": "V_th", "dSS": "SS", "dSS_profile": "SS_profile", "dlogIon": "I_on", "E_lin": "E_lin"}


def metric_errors(met: Mapping[str, Any]) -> dict[str, float]:
    """Signed, tolerance-normalised device-metric errors (1.0 = at the tolerance)."""
    out = {}
    out["dVth"] = met["dVth_V"] / C.TOL["dVth_V"] if met["dVth_V"] is not None else C.MISSING_METRIC_RESIDUAL
    if met["dSS_V_dec"] is not None and met["SS_meas_mV_dec"]:
        out["dSS"] = (1e3 * met["dSS_V_dec"] / met["SS_meas_mV_dec"]) / C.TOL["dSS_rel"]
    else:
        out["dSS"] = C.MISSING_METRIC_RESIDUAL
    out["dlogIon"] = met["dlogIon_dec"] / C.TOL["dlogIon_dec"]
    out["E_lin"] = met["E_lin"] / C.TOL["E_lin"]
    return out


def target_branch(me: Mapping[str, float]) -> bool:
    return all(abs(me[k]) <= 1.0 for k in METRICS)


def curve_terms(prep: MeasPrep, r: np.ndarray) -> dict[str, Any]:
    """Region-balanced curve terms of one curve: mean r_i^2 per region (subthreshold, turn_on, on) and the point
    weights (sum_i w_i r_i^2 over the curve = sum of its terms). Points below the detection limit join the
    subthreshold sum without counting in its point number: they add nothing while TCAD stays below the limit,
    and a TCAD leakage above the limit costs as much per point as a subthreshold error."""
    r2 = np.asarray(r, float) ** 2
    w = np.zeros(len(r2))
    out: dict[str, Any] = {}
    for reg in C.CURVE_REGIONS:
        m = prep.region == reg
        n = int(m.sum())
        idx = (m | (prep.region == "off")) if reg == "subthreshold" else m
        if n == 0:
            n = int(idx.sum())
        if n == 0:
            continue
        w[idx] = 1.0 / n
        out[reg] = {"value": float(np.sum(r2[idx]) / n), "n_points": n, "n_below_detection_limit":
                    int((idx & (prep.region == "off")).sum())}
    return {"terms": out, "weights": w}


def vth_shift(lo: Mapping[str, Any], hi: Mapping[str, Any]) -> dict[str, Any]:
    """Drain-induced V_th shift dV_th = V_th(V_D high) - V_th(V_D low), TCAD against measured. The tolerance is
    TOL_VTH_SHIFT_REL of the measured shift. A shift that cannot be extracted counts as MISSING_METRIC_RESIDUAL."""
    out = {"vd_low": lo["vd"], "vd_high": hi["vd"], "TCAD_mV": None, "measured_mV": None, "error_mV": None}
    if lo["Vth_meas_V"] is not None and hi["Vth_meas_V"] is not None:
        out["measured_mV"] = 1e3 * (hi["Vth_meas_V"] - lo["Vth_meas_V"])
    out["tolerance_mV"] = None if out["measured_mV"] is None else \
        max(1e3 * C.TOL_VTH_SHIFT_MIN_V, C.TOL_VTH_SHIFT_REL * abs(out["measured_mV"]))
    if lo["Vth_sim_V"] is not None and hi["Vth_sim_V"] is not None:
        out["TCAD_mV"] = 1e3 * (hi["Vth_sim_V"] - lo["Vth_sim_V"])
    if out["TCAD_mV"] is not None and out["measured_mV"] is not None:
        out["error_mV"] = out["TCAD_mV"] - out["measured_mV"]
        out["error"] = out["error_mV"] / out["tolerance_mV"]
    else:
        out["error"] = C.MISSING_METRIC_RESIDUAL
    return out


def _terms(out_b: Mapping[str, Any], cterms: Mapping[str, Any], metrics: Sequence[str] = METRICS_OBJECTIVE
           ) -> tuple[dict[str, float], dict[str, Any] | None]:
    """All J terms (squared, tolerance units), keyed '<metric>@<V_D>', 'V_th_shift' and 'region:<name>@<V_D>'.
    ``metrics``: the device metrics of J (METRICS_OBJECTIVE, with the SS profile)."""
    terms: dict[str, float] = {}
    for k, b in out_b.items():
        for m in metrics:
            terms[f"{TERM_NAMES[m]}@{b['vd']:g}"] = float(b["metric_errors"][m]) ** 2
    shift = None
    devs = {b["device"] for b in out_b.values()}
    if len(out_b) == 2 and len(devs) == 1:
        lo, hi = sorted(out_b.values(), key=lambda b: b["vd"])
        shift = vth_shift(lo, hi)
        terms["V_th_shift"] = float(shift["error"]) ** 2
    for k, b in out_b.items():
        for reg, t in cterms[k]["terms"].items():
            terms[f"region:{reg}@{b['vd']:g}"] = t["value"]
    return terms, shift


def score(preps: Sequence[MeasPrep], sims: Sequence[tuple[Sequence[float], Sequence[float]]],
          objective: str = "J_objective") -> dict[str, Any]:
    """Score one card on a set of curves (normally the two calibration curves): J, its terms, the target and the
    residual vector of the search."""
    if objective not in C.OBJECTIVES:
        raise ValueError(f"unknown objective {objective!r}")
    branches, out_b, cterms = [], {}, {}
    for prep, (sv, si) in zip(preps, sims):
        res = branch_residuals(prep, sv, si)
        met = metric_branch(sv, si, prep.vg, prep.i_raw, prep.ss_low_a)
        me = metric_errors(met)
        prof = ss_profile_branch(prep, res["i_sim"])
        me["dSS_profile"] = prof["error"]
        branches.append(res)
        key = f"{prep.device}_vd{vd_tag(prep.vd)}"
        cterms[key] = curve_terms(prep, res["r"])
        out_b[key] = {
            "device": prep.device, "vd": prep.vd, **met,
            "metric_errors": me, "target": target_branch(me),
            "regions": region_summary(prep, res), "curve_terms": cterms[key]["terms"], "ss_profile": prof,
            "sigma_I_A": prep.sigma_I, "loq_A": prep.loq,
            "curve": {"vg": prep.vg, "i_meas": prep.i_raw, "i_sim": res["i_sim"], "r": res["r"], "dg": res["dg"],
                      "sigma": res["sigma"]},
        }
    terms_all, shift = _terms(out_b, cterms, METRICS_OBJECTIVE)
    terms = {k: v for k, v in terms_all.items() if not (k == "V_th_shift" and not C.VTH_SHIFT_IN_J)}
    K = len(terms)
    point_r = np.concatenate([np.asarray(b["r"]) * np.sqrt(cterms[k]["weights"] / K) for b, k in zip(branches, out_b)])
    scalar_r = [b["metric_errors"][m] for b in out_b.values() for m in METRICS_OBJECTIVE]
    if shift is not None and "V_th_shift" in terms:
        scalar_r.append(shift["error"])
    R = np.concatenate([point_r, np.asarray(scalar_r, float) / math.sqrt(K)])
    return {"objective": objective, "J": float(np.mean(list(terms.values()))), "target": all(v <= 1.0 for v in terms.values()),
            "residual_vector": R, "residual_vector_unweighted": R, "terms": terms, "n_terms": K, "V_th_shift": shift,
            "branches": out_b}


def acceptance_terms(objective: str) -> list[str] | None:
    return list(C.J_TERMS) if objective == "J_objective" else None


def align(preps: Sequence[MeasPrep], sims: Sequence[tuple[np.ndarray, np.ndarray]], lo: float, hi: float,
          n_grid: int = 25, objective: str = "J_objective", loss: str | None = None,
          stats: dict | None = None) -> tuple[float, dict[str, Any]]:
    """Gate work-function alignment: the V_G shift d in [lo, hi] (shared by both V_D) that minimises J
    (``loss`` from config.ALIGNMENT_LOSSES). The returned score is J at that shift. A gate work function larger by d gives I_D(V_G) = I_D,ref(V_G - d), i.e. the curve
    moves by +d. ``stats`` (optional) receives the number of scorings."""
    from scipy.optimize import minimize_scalar
    if loss not in C.ALIGNMENT_LOSSES:
        raise ValueError(f"unknown alignment loss {loss!r}")
    stats = stats if stats is not None else {}
    stats.setdefault("n_score", 0)

    def sc_at(d: float) -> dict[str, Any]:
        stats["n_score"] += 1
        return score(preps, [(np.asarray(v, float) + d, i) for v, i in sims], objective)

    def J_at(d: float) -> float:
        return sc_at(d)["J"]
    if hi - lo < 1e-6:
        d = 0.5 * (lo + hi)
        return d, sc_at(d)
    grid = np.linspace(lo, hi, n_grid)
    js = [J_at(d) for d in grid]
    k = int(np.argmin(js))
    a, b = grid[max(k - 1, 0)], grid[min(k + 1, n_grid - 1)]
    res = minimize_scalar(J_at, bounds=(a, b), method="bounded", options={"xatol": 2e-4})
    d = float(res.x) if res.fun < js[k] else float(grid[k])
    return d, sc_at(d)


def gm_peak(b: Mapping[str, Any]) -> dict[str, Any]:
    c = b.get("curve") or {}
    if not c:
        return {}
    vg = np.asarray(c["vg"], float)
    out = []
    for key in ("i_sim", "i_meas"):
        g = np.gradient(np.abs(np.asarray(c[key], float)), vg)
        k = int(np.argmax(g))
        out.append((float(g[k]), float(vg[k])))
    (gs, vs), (gm, vm) = out
    return {"g_m_max_uS (TCAD, measured)": [round(1e6 * gs, 3), round(1e6 * gm, 3)],
            "V_G_at_g_m_max_V (TCAD, measured)": [round(vs, 3), round(vm, 3)],
            "dg_m_max_percent": round(100 * (gs / gm - 1), 1) if gm > 0 else None}


def brief(sc: Mapping[str, Any]) -> dict[str, Any]:
    """Compact, JSON-friendly summary in device terms (no per-point arrays)."""
    out = {"J": round(sc["J"], 5), "calibration_target_met": sc["target"],
           "J_terms": _terms_view(sc), "curves": {}}
    sh = sc.get("V_th_shift")
    if sh is not None:
        out["V_th_shift"] = {
            "definition": f"V_th(V_D = {sh['vd_high']:g} V) - V_th(V_D = {sh['vd_low']:g} V)",
            "TCAD_mV": None if sh["TCAD_mV"] is None else round(sh["TCAD_mV"], 1),
            "measured_mV": None if sh["measured_mV"] is None else round(sh["measured_mV"], 1),
            "error_mV": None if sh["error_mV"] is None else round(sh["error_mV"], 1),
            "tolerance_mV": None if sh["tolerance_mV"] is None else round(sh["tolerance_mV"], 1),
            "error_in_tolerance_units": round(sh["error"], 3)}
    for k, b in sc["branches"].items():
        me = b["metric_errors"]
        out["curves"][k] = {
            f"V_th_at_{C.VTH_LEVEL_A * 1e9:g}nA_V (TCAD, measured)": [None if b["Vth_sim_V"] is None else round(b["Vth_sim_V"], 4),
                                                None if b["Vth_meas_V"] is None else round(b["Vth_meas_V"], 4)],
            "dV_th_mV": None if b["dVth_V"] is None else round(1e3 * b["dVth_V"], 1),
            "SS_mV_dec (TCAD, measured)": [None if b["SS_sim_mV_dec"] is None else round(b["SS_sim_mV_dec"], 1),
                                          None if b["SS_meas_mV_dec"] is None else round(b["SS_meas_mV_dec"], 1)],
            "dSS_percent": None if b["dSS_V_dec"] is None or not b["SS_meas_mV_dec"] else
            round(100 * 1e3 * b["dSS_V_dec"] / b["SS_meas_mV_dec"], 1),
            "I_on_A (TCAD, measured)": [float(f"{b['Ion_sim_A']:.4g}"), float(f"{b['Ion_meas_A']:.4g}")],
            "I_on_uA_per_um (TCAD, measured)": [round(1e6 * b["Ion_sim_A"] / C.DEVICE_SPEC["W_um"], 3),
                                                round(1e6 * b["Ion_meas_A"] / C.DEVICE_SPEC["W_um"], 3)],
            "I_on_at_V_G_V": round(b["Ion_VG_V"], 4),
            "dlog10_I_on_dec": round(b["dlogIon_dec"], 4), "E_lin": round(b["E_lin"], 4),
            "metric_errors_in_tolerance_units": {kk: round(v, 3) for kk, v in me.items()},
            "all_metrics_within_tolerance": b["target"],
            "regions": _round_regions(b["regions"]),
            **gm_peak(b),
        }
        prof = b.get("ss_profile")
        if prof:
            out["curves"][k]["SS_profile"] = {
                "definition": "SS over one-decade windows in half-decade steps from the measured curve's quantification "
                              "limit to the V_th level (merged windows use their actual span); error = (TCAD - measured) "
                              f"/ ({C.SS_PROFILE['tol_rel']:g} x measured); the term is the worst window",
                "windows": [{"I_A": [w["lo_A"], w["hi_A"]], "SS_mV_dec (TCAD, measured)":
                             [None if w["SS_TCAD_mV_dec"] is None else round(w["SS_TCAD_mV_dec"], 1),
                              None if w["SS_meas_mV_dec"] is None else round(w["SS_meas_mV_dec"], 1)],
                             "error_in_tolerance_units": None if w["error_tol"] is None else round(w["error_tol"], 3),
                             "measured_points": w["n_points"], **({"merged": True} if w.get("merged") else {})}
                            for w in prof["windows"]],
                "worst_window_A": prof["worst_window_A"], "error_in_tolerance_units": round(prof["error"], 3),
                "SS_min_mV_dec (TCAD, measured)": [None if prof["SS_min_TCAD_mV_dec"] is None else round(prof["SS_min_TCAD_mV_dec"], 1),
                                                   None if prof["SS_min_meas_mV_dec"] is None else round(prof["SS_min_meas_mV_dec"], 1)]}
        out["curves"][k]["curve_error_by_region (J terms)"] = {r: round(t["value"], 4)
                                                               for r, t in b["curve_terms"].items()}
    return out


def _terms_view(sc: Mapping[str, Any]) -> dict[str, Any]:
    """The J terms in tolerance units: signed error for the scalar terms, mean r^2 for the curve regions."""
    rows = {}
    for name, v in sc["terms"].items():
        if name.startswith("region:"):
            rows[name] = {"term": round(v, 4), "within_tolerance": v <= 1.0}
            continue
        if name == "V_th_shift":
            e = sc["V_th_shift"]["error"]
        else:
            m, vd = name.split("@")
            key = next(k for k, n in TERM_NAMES.items() if n == m)
            e = next(b["metric_errors"][key] for b in sc["branches"].values() if f"{b['vd']:g}" == vd)
        rows[name] = {"error": round(float(e), 3), "term": round(v, 4), "within_tolerance": v <= 1.0}
    return {"definition": ("J = mean of the terms; each term = ((TCAD - measured) / tolerance)^2, or the mean r_i^2 "
                           "of a curve region"),
            "n_terms": sc["n_terms"],
            "outside_tolerance": [k for k, v in sc["terms"].items() if v > 1.0], "terms": rows}


def _round_regions(reg: Mapping[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in reg.items():
        if isinstance(v, dict):
            out[k] = {kk: (round(vv, 4) if isinstance(vv, float) else ([round(x, 3) for x in vv] if isinstance(vv, list) else vv))
                      for kk, vv in v.items()}
        else:
            out[k] = None if v is None else round(v, 5)
    return out
