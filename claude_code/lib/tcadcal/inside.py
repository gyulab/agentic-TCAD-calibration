"""
Every quantity is a prediction of the simulated model, not an independent measurement of the device.

Per bias point (off, 10 nA, the V_th level, on):
  * E_C - E_Fn (eV) at the probe points (channel centre at the gate interface, mid film and back interface; 10 nm inside
    each inner contact edge; 10 nm under each contact)
  * free electron sheet density (cm^-2) in the channel and under each contact; trapped electron sheet density (cm^-2)
    in the channel bulk and at each trap interface
  * transport-loss partition from electron quasi-Fermi-potential differences (fractions of V_D): source access (under
    the source contact to 10 nm inside the channel), channel, drain access. Reported only at and above V_th - 0.2 V
    (quasi-Fermi levels are ill defined where the electron density is negligible)
  * Fermi-level efficiency eta_F = d(E_Fn - E_C)/d(qV_G) at the channel centre, gate interface
  * quasi-static gate capacitance C_GG = dQ_G/dV_G relative to the gate-oxide capacitance of the gated area
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from . import config as C
from . import deck as D

EPS0 = 8.8541878128e-14   # F/cm
K_HFO2 = C.DEVICE_SPEC[C.K_GD_KEY]          # gate-dielectric permittivity (device.toml)
T_OX_CM = C.DEVICE_SPEC["t_gox_nm"] / 1e7    # gate-dielectric thickness, cm


def _col(df: pd.DataFrame, quantity: str, pt: tuple[float, float]) -> np.ndarray | None:
    for c in df.columns:
        if c.endswith(" " + quantity) and c.startswith("Pos("):
            xy = tuple(float(v) for v in c[4:c.index(")")].split(","))
            if abs(xy[0] - pt[0]) < 1e-6 and abs(xy[1] - pt[1]) < 1e-7:
                return df[c].to_numpy(float)
    return None


def _integral(df: pd.DataFrame, name: str) -> np.ndarray | None:
    cols = [c for c in df.columns if c.startswith(f"Integr{name} ")]
    return df[cols[0]].to_numpy(float) if cols else None


def _vg_at_current(vg: np.ndarray, i: np.ndarray, level: float) -> float | None:
    a = np.maximum(np.abs(i), 1e-30)
    idx = np.where((a[:-1] < level) & (a[1:] >= level))[0]
    if not len(idx):
        return None
    k = int(idx[-1])
    y0, y1 = math.log10(a[k]), math.log10(a[k + 1])
    return float(vg[k] + (math.log10(level) - y0) * (vg[k + 1] - vg[k]) / (y1 - y0)) if y1 != y0 else float(vg[k + 1])


def summarize(internal_csv: Path, device: str, vd: float) -> dict[str, Any]:
    df = pd.read_csv(internal_csv)
    df = df.sort_values("gate OuterVoltage").drop_duplicates("gate OuterVoltage", keep="last")
    vg = df["gate OuterVoltage"].to_numpy(float)
    i_d = df["drain TotalCurrent"].to_numpy(float)
    g = C.GEOMETRIES[C.DEVICES[device]["geometry"]]
    L, lov = g["L_um"], g["Lov_each_um"]
    pts = D.probe_points(device)
    vth = _vg_at_current(vg, i_d, C.VTH_LEVEL_A)
    v10 = _vg_at_current(vg, i_d, 1e-8)
    bias = {C.POINT_OFF: C.VG_START, "subthreshold (10 nA)": v10, C.POINT_VTH: vth, C.POINT_ON: C.VG_ION_V}
    um2_to_sheet = 1e-4   # integral over a window [cm^-3 um^2] / window length [um] -> cm^-3 um -> x1e-4 -> cm^-2
    sheets = {"n_channel": L, "n_under_source": lov, "n_under_drain": lov, "nt_channel": L}
    out: dict[str, Any] = {"V_D": vd, "V_th_V": vth, "points": {}, "note": "model predictions, not measurements"}
    ec = {k: _col(df, "ConductionBandEnergy", p) for k, p in pts.items()}
    ef = {k: _col(df, "eQuasiFermiEnergy", p) for k, p in pts.items()}
    phi = {k: _col(df, "eQuasiFermiPotential", p) for k, p in pts.items()}
    integ = {n: _integral(df, n) for n in sheets}
    for c in df.columns:
        if c.startswith("Integrnit_"):
            integ[c.split(" ")[0][len("Integr"):]] = df[c].to_numpy(float)
    qg = df["gate Charge"].to_numpy(float) if "gate Charge" in df else None
    cox_total = EPS0 * K_HFO2 / T_OX_CM * (C.DEVICE_SPEC["W_um"] * 1e-4) * ((2 * lov + L) * 1e-4)   # F, gated area
    for label, v in bias.items():
        if v is None or v < vg.min() - 1e-9 or v > vg.max() + 1e-9:
            out["points"][label] = None
            continue
        row: dict[str, Any] = {"V_G": round(v, 4)}
        row["Ec_minus_EFn_eV"] = {k: round(float(np.interp(v, vg, ec[k] - ef[k])), 4) for k in pts if ec[k] is not None
                                  and ef[k] is not None}
        sh = {}
        for n, arr in integ.items():
            if arr is None:
                continue
            length = sheets.get(n)
            if length is None:   # interface traps: the integral over the interface line is [cm^-2 um]
                length = (2 * lov + L)
            sh[n] = float(f"{float(np.interp(v, vg, arr)) * um2_to_sheet / length:.4g}") if n in sheets else \
                float(f"{float(np.interp(v, vg, arr)) / length:.4g}")
        row["sheet_density_cm2"] = sh
        if vth is not None and v >= vth - 0.2 and all(phi.get(k) is not None for k in
                                                      ("under_source_mid", "source_in_gate", "drain_in_gate", "under_drain_mid")) and vd > 0:
            p_s = abs(float(np.interp(v, vg, phi["source_in_gate"])))
            p_d = abs(float(np.interp(v, vg, phi["drain_in_gate"])))
            row["V_D_share"] = {"source_access": round(p_s / vd, 4), "channel": round((p_d - p_s) / vd, 4),
                                "drain_access": round((vd - p_d) / vd, 4)}
        if ec.get("centre_gate") is not None and ef.get("centre_gate") is not None:
            d = np.gradient(ef["centre_gate"] - ec["centre_gate"], vg)
            row["eta_F"] = round(float(np.interp(v, vg, d)), 4)
        if qg is not None:
            cgg = np.gradient(qg, vg)
            row["C_GG_over_C_ox"] = round(float(np.interp(v, vg, np.abs(cgg))) / cox_total, 4)
        out["points"][label] = row
    return out
