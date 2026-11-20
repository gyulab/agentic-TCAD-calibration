"""
Computed from what every run already stores (no extra solves): the snapshots written during the gate sweep
(`snapshots.json.gz`: lateral cuts along the channel, vertical cuts across the film) and the per-bias internal table
(`evaluation["inside"]`, from the harness CurrentPlot). Everything here is a prediction of the simulated model, not a
measurement.

Per drain bias and per bias point (off, 10 nA, V_th, on) it reports:
- the conduction-band maximum near the inner source edge, relative to the source Fermi level;
- the electron quasi-Fermi level drop split into source contact, channel and drain contact (fractions of V_D);
- the film flatness: E_C at the gate interface minus E_C at the back interface (channel centre);
- trapped charge along the channel (interface and bulk, at the source edge, centre and drain edge);
- the share of the electron current in the front half of the film (channel centre);
- an artifact screen: a conduction-band spike at an inner contact edge.
It also gives the same quantities at one fixed V_G for both drain biases (`drain_response_at_fixed_V_G`) and the
gate alignment and charge-budget estimates (`gate_alignment_estimates`). Observables and definitions only: the
interpretation is the agent's, with alternatives and limits.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from . import config as C
from .inside import EPS0, K_HFO2, T_OX_CM

Q = 1.602176634e-19
COX = EPS0 * K_HFO2 / T_OX_CM            # F/cm^2
ARTIFACT_EV = 0.10                         # a contact-edge E_C spike above both neighbours by more than this is flagged
POINTS = (C.POINT_OFF, "subthreshold (10 nA)", C.POINT_VTH, C.POINT_ON)


def _edges(device: str) -> tuple[float, float]:
    g = C.GEOMETRIES[C.DEVICES[device]["geometry"]]
    return g["Lov_each_um"], g["Lov_each_um"] + g["L_um"]


def _at(x: np.ndarray, y: np.ndarray, x0: float) -> float:
    return float(y[int(np.argmin(np.abs(x - x0)))])


def _semi(cut: Mapping[str, Any]) -> list[int]:
    return [k for k, r in enumerate(cut["region"]) if r == C.REGIONS["channel"]]


def _sheet(cut: Mapping[str, Any], field: str) -> float | None:
    """Integral over the film thickness of a volume density [cm^-3] -> sheet density [cm^-2]. The two boundary
    vertices are left out: Sentaurus also stores interface-trap charge there, which `_interface` reports."""
    if field not in cut:
        return None
    k = _semi(cut)
    if len(k) < 4:
        return None
    y = np.array([cut["y_nm"][i] for i in k], float) * 1e-7
    v = np.abs(np.array([cut[field][i] for i in k], float))
    o = np.argsort(y)
    y, v = y[o][1:-1], v[o][1:-1]
    return float(np.trapezoid(v, y))


def _interface(cut: Mapping[str, Any]) -> float | None:
    v = cut.get("eInterfaceTrappedCharge")
    if not v:
        return None
    return float(max(abs(float(x)) for x in v))


def snapshot_summary(s: Mapping[str, Any], device: str, vd: float, bulk_traps: bool = True) -> dict[str, Any]:
    xsi, xdi = _edges(device)
    out: dict[str, Any] = {"V_G_snapshot": round(float(s["VG_V"]), 3)}
    L = s["lateral"]["mid"]
    x = np.array(L["x_um"], float)
    ec = np.array(L["ConductionBandEnergy"], float)
    efn = np.array(L["eQuasiFermiEnergy"], float)
    win = (x > xsi - 0.02) & (x < xsi + 0.10)
    if win.any():
        i = int(np.argmax(np.where(win, ec, -1e9)))
        out["Ec_max_near_source_eV"] = round(float(ec[i]), 3)      # E_C max near the source edge, source E_F = 0
        out["Ec_max_near_source_x_um"] = round(float(x[i]), 3)
    out["Ec_minus_EFn_centre_eV"] = round(_at(x, ec, 0.5 * (xsi + xdi)) - _at(x, efn, 0.5 * (xsi + xdi)), 3)
    if vd > 0:
        e_s, e_cs, e_cd = 0.0, _at(x, efn, xsi + 0.01), _at(x, efn, xdi - 0.01)
        out["V_D_partition"] = {"source_contact": round((e_s - e_cs) / vd, 3), "channel": round((e_cs - e_cd) / vd, 3),
                                "drain_contact": round((e_cd + vd) / vd, 3)}
    V = s["vertical"]
    c = V["centre"]
    k = _semi(c)
    if k:
        ys = [c["y_nm"][i] for i in k]
        front, back = k[int(np.argmin(ys))], k[int(np.argmax(ys))]   # smallest y = gate side (bottom gate)
        out["film_Ec_front_minus_back_eV"] = round(float(c["ConductionBandEnergy"][front]) - float(c["ConductionBandEnergy"][back]), 4)
        if "eCurrentDensity" in c:
            yv = np.array(ys, float)
            j = np.abs(np.array([c["eCurrentDensity"][i] for i in k], float))
            o = np.argsort(yv)
            yv, j = yv[o], j[o]
            mid = 0.5 * (yv[0] + yv[-1])
            tot = float(np.trapezoid(j, yv))
            if tot > 0:
                fr = float(np.trapezoid(np.where(yv <= mid, j, 0.0), yv)) / tot
                out["current_front_half_fraction"] = round(fr, 3)
    traps = {}
    for cut in ("source_in", "centre", "drain_in"):
        if cut in V:
            it, bt = _interface(V[cut]), (_sheet(V[cut], "eTrappedCharge") if bulk_traps else None)
            if it is not None or bt is not None:
                traps[cut] = {k2: float(f"{v:.3g}") for k2, v in (("interface_cm2", it), ("bulk_cm2", bt)) if v is not None}
    if traps:
        out["trapped_charge"] = traps
    return out


def artifact_screen(snaps: list[Mapping[str, Any]], device: str) -> dict[str, Any]:
    """Largest conduction-band spike at an inner contact edge over all snapshots (both lateral cuts)."""
    xsi, xdi = _edges(device)
    worst = {"spike_eV": 0.0}
    for s in snaps:
        for cut in ("mid", "gate_side"):
            L = s["lateral"].get(cut)
            if not L:
                continue
            x = np.array(L["x_um"], float)
            ec = np.array(L["ConductionBandEnergy"], float)
            for edge, inside, outside in ((xsi, xsi + 0.05, xsi - 0.03), (xdi, xdi - 0.05, xdi + 0.03)):
                w = (x > edge - 0.015) & (x < edge + 0.015)
                if not w.any():
                    continue
                peak = float(ec[w].max())
                spike = peak - max(_at(x, ec, inside), _at(x, ec, outside))
                if spike > worst["spike_eV"]:
                    worst = {"spike_eV": round(spike, 3), "x_um": round(float(x[w][np.argmax(ec[w])]), 4),
                             "V_G": round(float(s["VG_V"]), 3), "cut": cut}
    worst["flag"] = worst["spike_eV"] > ARTIFACT_EV
    return worst


def gate_alignment_estimates(ev: Mapping[str, Any], chi_eV: float | None,
                             summary_rows: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    al = ev.get("alignment") or {}
    vals = ev.get("values_rendered") or ev.get("values") or {}
    if "WF_gate" not in vals:
        return None
    phi_g = float(vals["WF_gate"]) + float(al.get("shift_V") or 0.0)
    n_trap, basis = 0.0, "no trapped charge in the model"
    row = (((summary_rows or {}).get(C.VD_KEYS[0]) or {}).get(C.POINT_VTH) or {})
    tc = (row.get("trapped_charge") or {}).get("centre") or {}
    if tc:
        n_trap = float(tc.get("interface_cm2", 0.0)) + float(tc.get("bulk_cm2", 0.0))
        basis = f"channel centre, snapshot V_G {row.get('V_G_snapshot')} V (V_th {row.get('V_G_target')} V, V_D {C.VD_KEYS[0]} V)"
    t_um = C.DEVICE_SPEC[C.T_CH_KEY] * 1e-3
    n_don = float(vals.get("Nd_eff", 0.0)) * t_um * 1e-4
    dv_trap, dv_don = Q * n_trap / COX, Q * n_don / COX
    out = {"phi_G_aligned_eV": round(phi_g, 3), f"chi_{C.CHANNEL}_eV": chi_eV,
           "trapped_sheet_estimate_cm2": float(f"{n_trap:.3g}"), "trapped_basis": basis,
           "donor_sheet_cm2": float(f"{n_don:.3g}"),
           "dV_trapped_estimate_V": round(dv_trap, 3), "dV_donor_estimate_V": round(dv_don, 3),
           "phi_G_charge_compensated_estimate_eV": round(phi_g + dv_trap - dv_don, 3), "C_ox_F_cm2": float(f"{COX:.4g}"),
           "definitions": ("dV = q N / C_ox for the listed sheet charge. phi_G_charge_compensated_estimate_eV = phi_G_aligned "
                           "+ dV_trapped - dV_donor: an approximate estimate from a local, bias-dependent charge; not V_FB and "
                           "not an extracted work function.")}
    if chi_eV is not None:
        out["phi_G_minus_chi_eV"] = round(phi_g - chi_eV, 3)
    if "WF_sd" in vals and chi_eV is not None:
        out["phi_SD_minus_chi_eV"] = round(float(vals["WF_sd"]) - chi_eV, 3)
    return out


def drain_response_fixed_vg(snaps_by_vd: Mapping[str, list], device: str, vg_ref: float | None,
                            bulk_traps: bool) -> dict[str, Any] | None:
    """The same card, cut and gate voltage at the two calibration drain biases (energies relative to the source Fermi
    level): only the drain bias changes. vg_ref is the snapshot V_G nearest the threshold at the lower V_D."""
    if vg_ref is None or not all(k in snaps_by_vd for k in C.VD_KEYS):
        return None
    rows = {}
    for vd_s, snaps in snaps_by_vd.items():
        s = min(snaps, key=lambda z: abs(float(z["VG_V"]) - vg_ref))
        rows[vd_s] = snapshot_summary(s, device, float(vd_s), bulk_traps)
    a, b = rows[C.VD_KEYS[0]], rows[C.VD_KEYS[1]]
    if abs(a["V_G_snapshot"] - b["V_G_snapshot"]) > 1e-6:
        return None
    def trap(r, cut):
        return sum((r.get("trapped_charge") or {}).get(cut, {}).values()) if r.get("trapped_charge") else 0.0
    out = {"V_G": a["V_G_snapshot"]}
    for q, f in (("Ec_max_near_source_eV", lambda r: r.get("Ec_max_near_source_eV")),
                 ("Ec_minus_EFn_centre_eV", lambda r: r.get("Ec_minus_EFn_centre_eV")),
                 ("trapped_centre_cm2", lambda r: trap(r, "centre")), ("trapped_drain_edge_cm2", lambda r: trap(r, "drain_in")),
                 ("trapped_source_edge_cm2", lambda r: trap(r, "source_in"))):
        va, vb = f(a), f(b)
        out[q] = {"V_D_0.05": va, "V_D_1": vb, "change": None if va is None or vb is None else float(f"{vb - va:.3g}")}
    return out


def summary(ev: Mapping[str, Any], bulk_traps: bool = True) -> dict[str, Any] | None:
    """The band and internal summary of an evaluation (None when it has no snapshots). ``bulk_traps``: the deck has
    trap states in the channel bulk (otherwise only interface-trap charge is reported)."""
    snaps_paths = ev.get("snapshots") or {}
    if not snaps_paths:
        return None
    device = next(iter(ev["branches"].values()))["device"] if ev.get("branches") else C.CALIBRATION[0][0]
    out: dict[str, Any] = {"note": "observables of the simulated model (snapshots and the per-bias table), not measurements",
                           "per_V_D": {}}
    snaps_by_vd: dict[str, list] = {}
    chi = None
    worst = {"spike_eV": 0.0, "flag": False}
    for vd_s, p in sorted(snaps_paths.items(), key=lambda kv: float(kv[0])):
        if not Path(p).exists():
            continue
        snaps = json.load(gzip.open(p))
        if not snaps:
            continue
        snaps_by_vd["1" if abs(float(vd_s) - 1.0) < 1e-9 else vd_s] = snaps
        mat = snaps[0].get(C.CH_MAT_KEY) or {}
        aff = mat.get("ElectronAffinity")
        if aff:
            chi = float(aff[0]) if isinstance(aff, (list, tuple)) else float(aff)
        vd = float(vd_s)
        ins_pts = (((ev.get("inside") or {}).get(vd_s) or {}).get("points") or {})
        rows = {}
        for label in POINTS:
            target = {C.POINT_OFF: C.VG_START, C.POINT_ON: C.VG_ION_V}.get(label)
            if target is None:
                pt = ins_pts.get(label)
                if not pt:
                    rows[label] = None
                    continue
                target = float(pt["V_G"])
            s = min(snaps, key=lambda z: abs(float(z["VG_V"]) - target))
            row = {"V_G_target": round(target, 3), **snapshot_summary(s, device, vd, bulk_traps)}
            pt = ins_pts.get(label) or {}
            for k in ("eta_F", "C_GG_over_C_ox", "V_D_share"):
                if k in pt:
                    row[k] = pt[k]
            sd = pt.get("sheet_density_cm2") or {}
            if "n_channel" in sd:
                row["n_channel_cm2"] = sd["n_channel"]
            if "V_D_share" in row:      # one V_D split per point: the per-bias table at the exact V_G wins
                row.pop("V_D_partition", None)
            rows[label] = row
        out["per_V_D"][vd_s] = rows
        a = artifact_screen(snaps, device)
        if a["spike_eV"] >= worst.get("spike_eV", 0.0):
            worst = {**a, "V_D": vd}
    out["contact_edge_artifact"] = worst
    vth05 = ((((ev.get("inside") or {}).get(C.VD_KEYS[0]) or {}).get("points") or {}).get(C.POINT_VTH) or {}).get("V_G")
    vg_ref = None
    if vth05 is not None and C.VD_KEYS[0] in snaps_by_vd:
        vg_ref = float(min(snaps_by_vd[C.VD_KEYS[0]], key=lambda z: abs(float(z["VG_V"]) - float(vth05)))["VG_V"])
    out["drain_response_at_fixed_V_G"] = drain_response_fixed_vg(snaps_by_vd, device, vg_ref, bulk_traps)
    out["gate_alignment_estimates"] = gate_alignment_estimates(ev, chi, out["per_V_D"])
    return out


KEY_POINTS = (C.POINT_VTH, C.POINT_ON)


def compact(b: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The few numbers a step or probe changes inside the device, at V_th and on, for each V_D."""
    if not b:
        return None
    out: dict[str, Any] = {}
    for vd, rows in (b.get("per_V_D") or {}).items():
        for lab in KEY_POINTS:
            r = (rows or {}).get(lab)
            if not r:
                continue
            tc = (r.get("trapped_charge") or {})
            out[f"{lab.split(' ')[0]}@{vd}"] = {
                "Ec_max_near_source_eV": r.get("Ec_max_near_source_eV"),
                "Ec_minus_EFn_centre_eV": r.get("Ec_minus_EFn_centre_eV"),
                "source_contact_share_of_V_D": (r.get("V_D_partition") or {}).get("source_contact"),
                "film_Ec_front_minus_back_eV": r.get("film_Ec_front_minus_back_eV"),
                "trapped_centre_cm2": sum(v for v in (tc.get("centre") or {}).values()) if tc else 0.0,
                "trapped_drain_edge_cm2": sum(v for v in (tc.get("drain_in") or {}).values()) if tc else 0.0}
    return out


def change(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What changed inside from evaluation a to evaluation b (compact numbers, b - a)."""
    ca, cb = compact(a), compact(b)
    if not ca or not cb:
        return None
    out = {}
    for k in cb:
        if k not in ca:
            continue
        d = {}
        for q, v in cb[k].items():
            u = ca[k].get(q)
            if v is not None and u is not None:
                d[q] = float(f"{v - u:.3g}")
        out[k] = d
    return out
