"""
Internal quantities (band edges, charge, quasi-Fermi potentials) are predictions of the simulated model. They locate where
charge and voltage go in the model and suggest discriminating checks; they are never evidence that a mechanism is right.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from . import config as C
from . import evaluation as E
from . import jobs as J
from . import measurements as M
from .state import StateError, Workspace

NOTE = ("Internal quantities are predictions of the simulated model, not measurements. Use them to locate where charge and "
        "voltage go and to design a discriminating check; they are not evidence that a mechanism is right.")
Q = 1.602176634e-19
KT_300 = 0.025852
C_OX = C.C_OX_F_CM2     # F/cm^2, gate dielectric of device.toml


def _emit(tool: str, argv: list[str], fn) -> int:
    from .cli import _emit as emit
    return emit(tool, argv, fn)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def main_verify(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="verify", description="Verify an evaluation's card on the refined mesh (all mesh "
                                 "spacings halved; 2 runs, counted as verification runs). A pass adds the card to the "
                                 "numerically verified range of its deck version, so it can be accepted")
    ap.add_argument("eval", nargs="?", help="evaluation id (E#####)")
    ap.add_argument("--status", metavar="DECK", help="show the verified range record of a deck version")
    a = ap.parse_args(argv)

    def run() -> Any:
        ws = Workspace(Path.cwd())
        if a.status:
            d = E.deck_flags(ws, a.status)
            return {"deck": a.status, "verified_cards": [c.get("eval") for c in d.get("verified", [])],
                    "mesh_sensitive": d.get("mesh_sensitive"), "unverifiable": d.get("unverifiable"),
                    "rule": "a card counts as verified within 0.5 dec (densities), 0.05 eV (energies) or 25 % (other "
                            "parameters) of a verified card"}
        if not a.eval:
            raise ValueError("give an evaluation id")
        if ws.rule("fixed_mesh"):
            return {"refused": J.FIXED_MESH_NOTE}
        ev = E.load_eval(ws, a.eval)
        if ev.get("status") != "ok":
            raise ValueError(f"{a.eval} did not converge; there is nothing to verify")
        jid = J.submit(ws, "verify", {"eval": a.eval, "deck": ev["deck"]})
        return {"job": jid, "next": f"End your turn with the line: WAIT_JOB {jid}"}
    return _emit("verify", argv, run)


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------
def _snapshots(ev: dict[str, Any], vd: str) -> list[dict[str, Any]]:
    path = (ev.get("snapshots") or {}).get(vd)
    if not path or not Path(path).exists():
        return []
    with gzip.open(path, "rt") as fh:
        return json.load(fh)


def _nearest(snaps: list[dict[str, Any]], vg: float) -> dict[str, Any] | None:
    ok = [s for s in snaps if s.get("VG_V") is not None]
    return min(ok, key=lambda s: abs(s["VG_V"] - vg)) if ok else None


def band_table(snap: dict[str, Any]) -> dict[str, Any]:
    """Compact band-diagram table of one snapshot: E_C - E_Fn and electron density across the channel film on each vertical
    cut (gate interface, mid film, back interface), plus the conduction-band step under the contacts."""
    out = {"V_G": snap.get("VG_V"), "cuts": {}}
    for name, cut in snap["vertical"].items():
        reg = cut.get("region", [])
        idx = [i for i, r in enumerate(reg) if r == C.REGIONS["channel"]]
        if not idx:
            continue
        y = [cut["y_nm"][i] for i in idx]
        ec = [cut["ConductionBandEnergy"][i] for i in idx]
        ef = [cut["eQuasiFermiEnergy"][i] for i in idx]
        n = [cut.get("eDensity", [None] * len(reg))[i] for i in idx]
        pick = {"gate interface": 0, "mid film": len(idx) // 2, "back interface or contact": len(idx) - 1}
        out["cuts"][name] = {k: {"y_nm": round(y[j], 3), "Ec_minus_EFn_eV": round(ec[j] - ef[j], 4),
                                 "n_cm3": None if n[j] is None else float(f"{n[j]:.3g}")} for k, j in pick.items()}
        out["cuts"][name]["band_bending_across_film_eV"] = round(ec[-1] - ec[0], 4)
    return out


def plot_bands(path: Path, ev: dict[str, Any], vd: str, vgs: list[float]) -> list[float]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    snaps = _snapshots(ev, vd)
    picked = []
    fig, ax = plt.subplots(2, 3, figsize=(12, 7))
    for k, vg in enumerate(vgs):
        s = _nearest(snaps, vg)
        if s is None:
            continue
        picked.append(s["VG_V"])
        col = f"C{k}"
        for j, name in enumerate(("source_in", "centre", "drain_in")):
            cut = s["vertical"][name]
            idx = [i for i, r in enumerate(cut["region"]) if r == C.REGIONS["channel"]]
            y = [cut["y_nm"][i] for i in idx]
            ax[0, j].plot(y, [cut["ConductionBandEnergy"][i] for i in idx], "-", color=col, label=f"V_G {s['VG_V']:.2f} V")
            ax[0, j].plot(y, [cut["eQuasiFermiEnergy"][i] for i in idx], "--", color=col)
            ax[0, j].set_title(f"{name} (vertical cut, {C.CHANNEL} film)")
            ax[0, j].set_xlabel("y from gate interface (nm)")
            ax[0, j].set_ylabel("E_C (solid), E_Fn (dashed) (eV)")
        for j, name in enumerate(("gate_side", "mid")):
            lat = s["lateral"][name]
            ax[1, j].plot(lat["x_um"], lat["ConductionBandEnergy"], "-", color=col)
            ax[1, j].plot(lat["x_um"], lat["eQuasiFermiEnergy"], "--", color=col)
            ax[1, j].set_title(f"lateral cut, {name} of the {C.CHANNEL} film")
            ax[1, j].set_xlabel("x (um)")
        lat = s["lateral"]["gate_side"]
        ax[1, 2].semilogy(lat["x_um"], np.maximum(np.abs(lat["eDensity"]), 1e0), "-", color=col)
        ax[1, 2].set_title("electron density, gate side")
        ax[1, 2].set_xlabel("x (um)")
    ax[0, 0].legend(fontsize=7)
    fig.suptitle(f"{ev['eval_id']} V_D = {vd} V: model band diagrams (predictions of the simulated model)", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=85)
    plt.close(fig)
    return picked


def observability(ev: dict[str, Any]) -> dict[str, Any]:
    """Range of E_C - E_Fn at the gate interface (channel centre) swept by the measured V_G range at each V_D: sub-gap
    states outside this window are not probed by the calibration curves."""
    out = {}
    for vd, ins in (ev.get("inside") or {}).items():
        run = next((r for r in ev["runs"] if f"{r['vd']:g}" == vd), None)
        if not run or not run.get("run_dir"):
            continue
        import pandas as pd
        df = pd.read_csv(Path(run["run_dir"]) / "internal.csv")
        from . import deck as D
        x, y = D.probe_points(run["device"])["centre_gate"]
        ec = next((c for c in df.columns if c.startswith(f"Pos({x:g},{y:g}) ConductionBandEnergy")), None)
        ef = next((c for c in df.columns if c.startswith(f"Pos({x:g},{y:g}) eQuasiFermiEnergy")), None)
        if not ec or not ef:
            continue
        shift = (ev.get("alignment") or {}).get("shift_V") or 0.0
        vg = df["gate OuterVoltage"].to_numpy(float) + shift   # aligned onto the measured V_G axis
        m = (vg >= C.VG_START - 1e-9) & (vg <= C.VG_STOP + 1e-9)
        d = (df[ec] - df[ef]).to_numpy(float)[m]
        out[vd] = {"Ec_minus_EFn_range_eV": [round(float(d.min()), 4), round(float(d.max()), 4)],
                   "meaning": "states deeper than the upper value below E_C stay filled over the whole measured sweep; the "
                              "curves constrain only states whose occupancy changes inside this window"}
    return out


def _diff_inside(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for vd in set(a.get("inside") or {}) & set(b.get("inside") or {}):
        pa, pb = a["inside"][vd]["points"], b["inside"][vd]["points"]
        rows = {}
        for lab in pa:
            if not pa.get(lab) or not pb.get(lab):
                continue
            r = {}
            for k in ("eta_F", "C_GG_over_C_ox"):
                if k in pa[lab] and k in pb[lab]:
                    r[k] = round(pb[lab][k] - pa[lab][k], 4)
            r["Ec_minus_EFn_eV"] = {p: round(pb[lab]["Ec_minus_EFn_eV"][p] - v, 4)
                                    for p, v in pa[lab]["Ec_minus_EFn_eV"].items() if p in pb[lab]["Ec_minus_EFn_eV"]}
            r["sheet_density_ratio"] = {p: (None if not v else round(pb[lab]["sheet_density_cm2"].get(p, 0) / v, 4))
                                        for p, v in pa[lab]["sheet_density_cm2"].items()}
            if "V_D_share" in pa[lab] and "V_D_share" in pb[lab]:
                r["V_D_share_change"] = {p: round(pb[lab]["V_D_share"][p] - v, 4) for p, v in pa[lab]["V_D_share"].items()}
            rows[lab] = r
        out[vd] = rows
    return out


def main_diagnose(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="diagnose", description="Inside the device (model predictions): charge budget, "
                                 "E_C - E_Fn at probe points, transport-loss partition from quasi-Fermi potentials, "
                                 "Fermi-level efficiency, gate capacitance, band diagrams from the sweep snapshots")
    ap.add_argument("--show", metavar="EVAL", help="inside-the-device table of an evaluation (both V_D)")
    ap.add_argument("--bands", metavar="EVAL", help="band diagrams from the snapshots of an evaluation (writes a PNG)")
    ap.add_argument("--vd", default="1", help="drain bias of --bands / --run (0.05 or 1)")
    ap.add_argument("--vg", default="", help="comma-separated V_G values for --bands (nearest snapshots; default: "
                    "off, threshold, on)")
    ap.add_argument("--compare", nargs=2, metavar=("EVAL_A", "EVAL_B"), help="what changed inside from A to B")
    ap.add_argument("--observability", metavar="EVAL", help="E_C - E_Fn window swept by the measured V_G range")
    ap.add_argument("--run", metavar="EVAL", help="extra diagnostic run with snapshots every 75 mV (counted in the "
                    "diagnostic runs, inside the total budget)")
    a = ap.parse_args(argv)

    def run() -> Any:
        ws = Workspace(Path.cwd())
        if a.show:
            ev = E.load_eval(ws, a.show)
            if not ev.get("inside"):
                raise ValueError(f"{a.show} has no inside-the-device record (not converged)")
            return {"eval": a.show, "validity": ev.get("validity"), "inside": ev["inside"],
                    "activation": {k: v["label"] for k, v in (ev.get("activation") or {}).items()}, "note": NOTE}
        if a.bands:
            ev = E.load_eval(ws, a.bands)
            vd = f"{float(a.vd):g}"
            snaps = _snapshots(ev, vd)
            if not snaps:
                raise ValueError(f"{a.bands} has no snapshots at V_D {vd} V")
            if a.vg:
                vgs = [float(v) for v in a.vg.split(",")]
            else:
                vth = ((ev.get("inside") or {}).get(vd) or {}).get("V_th_V")
                vgs = [-1.0] + ([vth] if vth is not None else []) + [2.0]
            png = ws.root / "evals" / f"{a.bands}_bands_vd{vd}.png"
            picked = plot_bands(png, ev, vd, vgs)
            tables = [band_table(_nearest(snaps, v)) for v in picked]
            return {"eval": a.bands, "V_D": vd, "snapshot_VG_available": [s.get("VG_V") for s in snaps],
                    "tables": tables, "plot": str(png.relative_to(ws.root)), "note": NOTE}
        if a.compare:
            ea, eb = (E.load_eval(ws, x) for x in a.compare)
            return {"from": a.compare[0], "to": a.compare[1], "change_inside": _diff_inside(ea, eb), "note": NOTE}
        if a.observability:
            return {"eval": a.observability, "window": observability(E.load_eval(ws, a.observability)), "note": NOTE}
        if a.run:
            ev = E.load_eval(ws, a.run)
            jid = J.submit(ws, "diag", {"eval": a.run, "vd": float(a.vd), "deck": ev["deck"]})
            return {"job": jid, "next": f"End your turn with the line: WAIT_JOB {jid}",
                    "budget": "diagnostic runs are counted inside the total budget (at most 40)"}
        raise ValueError("give one of --show, --bands, --compare, --observability, --run")
    return _emit("diagnose", argv, run)


# ---------------------------------------------------------------------------
# vd_consistency: long-channel (gradual-channel / Pao-Sah) test
# ---------------------------------------------------------------------------
def longchannel_predict(vg: np.ndarray, i_lo: np.ndarray, vd_lo: float, vd_hi: float) -> np.ndarray:
    """I(V_G, V_D,hi) predicted from the V_D,lo curve: with traps in equilibrium with the local quasi-Fermi level the local
    sheet conductance depends only on V_G - V, so I(V_G, V_D) = K int_{V_G - V_D}^{V_G} G(x) dx, and the low-V_D curve
    measures G: K G(x) = I(x + V_D,lo/2, V_D,lo) / V_D,lo. Holds for any trap DOS and any density-dependent mobility; it
    fails when the current is not set by the channel conductance alone, when the local conductance depends on more than
    V_G - V, or when the curves are not steady states independent of the sweep history."""
    lg = np.log10(np.clip(np.abs(i_lo), 1e-14, None))
    x = np.linspace(vg.min() - vd_hi, vg.max(), 6001)
    slope = (lg[-1] - lg[-2]) / (vg[-1] - vg[-2])
    g = 10 ** np.interp(x + vd_lo / 2, vg, lg, left=lg[0], right=lg[-1] + slope * vd_lo / 2) / vd_lo
    cum = np.concatenate([[0.0], np.cumsum(0.5 * (g[1:] + g[:-1]) * np.diff(x))])
    return np.interp(vg, x, cum) - np.interp(vg - vd_hi, x, cum)


def vd_consistency(vg: np.ndarray, i_lo: np.ndarray, i_hi: np.ndarray, vd_lo: float, vd_hi: float,
                   regions: dict[str, tuple[float, float]]) -> dict[str, Any]:
    ip = longchannel_predict(vg, i_lo, vd_lo, vd_hi)
    ok = vg <= vg.max() - 0.04
    d = np.log10(np.clip(np.abs(i_hi), 1e-14, None)) - np.log10(np.clip(ip, 1e-14, None))
    out = {}
    for r, (lo, hi) in regions.items():
        m = ok & (vg >= lo - 1e-6) & (vg <= hi + 1e-6)
        if m.any():
            out[r] = {"V_G_window": [round(lo, 3), round(hi, 3)], "mean_excess_dec": round(float(d[m].mean()), 4),
                      "range_dec": [round(float(d[m].min()), 4), round(float(d[m].max()), 4)]}
    return out


def _regions_from_measured(vg: np.ndarray, i: np.ndarray, loq: float) -> dict[str, tuple[float, float]]:
    a = np.abs(i)
    ion = float(np.max(a))
    reg = {}
    for name, lo_i, hi_i in (("subthreshold", loq, C.VTH_LEVEL_A), ("turn_on", C.VTH_LEVEL_A, ion / 10), ("on", ion / 10, 10 * ion)):
        m = (a >= lo_i) & (a < hi_i)
        if m.any():
            reg[name] = (float(vg[m].min()), float(vg[m].max()))
    return reg


def main_vd_consistency(argv: list[str]) -> int:
    lo_s, hi_s = C.VD_KEYS
    ap = argparse.ArgumentParser(prog="vd_consistency", description=f"Long-channel test: predict the V_D = {hi_s} V curve from "
                                 f"the V_D = {lo_s} V curve (gradual channel, local conductance a function of V_G - V only) "
                                 f"and report the excess of the actual {hi_s} V curve, for the measured curves and for a TCAD "
                                 "evaluation. An excess that the model cannot reproduce cannot come from the trap DOS, the "
                                 "donor density or a density-dependent mobility")
    ap.add_argument("--eval", help="also test this evaluation's TCAD curves (aligned onto the measured V_G axis)")
    a = ap.parse_args(argv)

    def run() -> Any:
        ws = Workspace(Path.cwd())
        (dev_lo, vd_lo), (dev_hi, vd_hi) = sorted(ws.calibration, key=lambda c: c[1])
        lo, hi = M.load_forward(dev_lo, vd_lo), M.load_forward(dev_hi, vd_hi)
        vg = np.asarray(lo.vg, float)
        if not np.allclose(vg, hi.vg, atol=1e-3):
            raise ValueError("the two calibration curves do not share a V_G grid")
        prep = E.calibration_preps(ws.program)[1]
        regions = _regions_from_measured(np.asarray(hi.vg), hi.i_abs, prep.loq)
        lo_s, hi_s = C.VD_KEYS
        out = {"definition": f"excess = log10 I({hi_s} V) actual - log10 I({hi_s} V) predicted from the {lo_s} V curve (dec)",
               "measured": vd_consistency(vg, lo.i_abs, hi.i_abs, vd_lo, vd_hi, regions),
               "regions_from": f"the measured {hi_s} V curve (detection limit to {C.VTH_LEVEL_A * 1e9:g} nA, to I_on/10, above)"}
        if a.eval:
            ev = E.load_eval(ws, a.eval)
            if ev.get("status") != "ok":
                raise ValueError(f"{a.eval} did not converge")
            by_vd = {float(b["vd"]): ev["curves"][k] for k, b in ev["branches"].items()}
            c_lo, c_hi = by_vd[float(vd_lo)], by_vd[float(vd_hi)]
            out["tcad"] = {"eval": a.eval, **vd_consistency(np.asarray(c_hi["vg"], float), np.asarray(c_lo["i_sim"], float),
                                                             np.asarray(c_hi["i_sim"], float), vd_lo, vd_hi, regions)}
        out["reading"] = ("an excess in the measured row that the TCAD row does not show cannot come from the channel "
                          "sub-gap states, the donor density or a density-dependent mobility: it breaks an assumption of the "
                          "test (current set by the channel conductance alone; local conductance a function of V_G - V only; "
                          "steady state independent of the sweep history)")
        return out
    return _emit("vd_consistency", argv, run)


# ---------------------------------------------------------------------------
# extract: the same extraction on measured and TCAD curves
# ---------------------------------------------------------------------------
def extract_curve(vg: np.ndarray, i: np.ndarray, vd: float, loq: float) -> dict[str, Any]:
    vg = np.asarray(vg, float)
    a = np.abs(np.asarray(i, float))
    g = C.GEOMETRIES["L1_lov1p5"]
    out: dict[str, Any] = {}
    lg = np.log10(np.clip(a, 1e-14, None))
    ss = np.gradient(vg, lg)                     # V/dec, local inverse slope
    m = (a >= loq) & (a <= C.VTH_LEVEL_A)
    if m.any():
        s_mv = 1e3 * ss[m]
        n_t = C_OX / Q * (ss[m] / (KT_300 * math.log(10)) - 1.0)    # cm^-2 eV^-1, effective (bulk + interface)
        out["subthreshold"] = {"V_G": [round(float(v), 3) for v in vg[m]], "SS_mV_dec": [round(float(v), 1) for v in s_mv],
                               "N_t_eff_cm2_eV": [float(f"{v:.3g}") for v in n_t],
                               "definition": "N_t,eff = (C_ox/q)(SS/(kT ln10/q) - 1): states that respond within a kT step "
                                             "of the Fermi level, bulk and interface lumped, per unit gate area"}
    if vd <= 0.1:
        gm = np.gradient(a, vg)
        mu = gm * g["L_um"] / (C.DEVICE_SPEC["W_um"] * C_OX * vd)
        k = int(np.argmax(mu))
        out["mu_FE_linear"] = {"max_cm2_Vs": round(float(mu[k]), 3), "at_V_G": round(float(vg[k]), 3),
                               "definition": f"g_m L / (W C_ox V_D) with L = {g['L_um']:g} um (the S/D gap), C_ox of "
                                             f"{C.DEVICE_SPEC['t_gox_nm']:g} nm {C.GATE_DIELECTRIC}; gated "
                                             "overlap contacts and series resistance bias it"}
    return out


def main_extract(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="extract", description="The same extraction on the measured curves and on a TCAD "
                                 "evaluation: local SS(V_G), effective sub-gap state density N_t,eff(V_G) from SS, linear "
                                 "field-effect mobility. Approximate textbook relations, stated with their assumptions")
    ap.add_argument("--eval", help="also extract from this evaluation's TCAD curves")
    a = ap.parse_args(argv)

    def run() -> Any:
        ws = Workspace(Path.cwd())
        preps = E.calibration_preps(ws.program)
        out: dict[str, Any] = {"measured": {}}
        for (dev, vd), prep in zip(ws.calibration, preps):
            fw = M.load_forward(dev, vd)
            out["measured"][f"{vd:g}"] = extract_curve(np.asarray(fw.vg), fw.i_abs, vd, prep.loq)
        if a.eval:
            ev = E.load_eval(ws, a.eval)
            out["tcad"] = {"eval": a.eval}
            for k, c in (ev.get("curves") or {}).items():
                vd = float(ev["branches"][k]["vd"])
                prep = preps[[float(v) for _, v in ws.calibration].index(vd)]
                out["tcad"][f"{vd:g}"] = extract_curve(np.asarray(c["vg"]), np.asarray(c["i_sim"]), vd, prep.loq)
        out["note"] = "approximate relations; compare measured and TCAD rows with the same assumptions, not absolute values"
        return out
    return _emit("extract", argv, run)


# ---------------------------------------------------------------------------
# identify
# ---------------------------------------------------------------------------
def main_identify(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="identify", description="Identifiability and sensitivity from a finished probe job "
                                 "(ofat): collinear parameter pairs, singular values, weakest direction, activation and "
                                 "LOW_SENSITIVITY labels")
    ap.add_argument("job", help="a finished ofat job id (J####)")
    a = ap.parse_args(argv)

    def run() -> Any:
        ws = Workspace(Path.cwd())
        res = J.read_json(ws.root / "jobs" / a.job / "result.json")
        if not res or "identifiability" not in res:
            raise ValueError(f"{a.job} is not a finished probe job with an identifiability record")
        return {"job": a.job, "identifiability": res["identifiability"],
                "activation_and_sensitivity": res.get("activation_and_sensitivity"),
                "reading": "a collinear pair cannot be separated by these two curves: report it as a combination; a "
                           "LOW_SENSITIVITY parameter is applied but barely moves J at this card"}
    return _emit("identify", argv, run)
