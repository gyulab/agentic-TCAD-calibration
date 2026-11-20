"""Measured forward I_D-V_G branches (CSV files with columns VG_V, VD_V and ID_drain_A).

Only the forward branch is ever returned. The files are acquisition-ordered: the forward branch is the
first monotonically increasing V_G segment (config.VG_START -> config.VG_STOP, or from the device's own
start, config.vg_start). A dual sweep is allowed: the first negative V_G step starts the reverse branch,
which is discarded.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C


def vd_file_tag(vd: float) -> str:
    return "p" + f"{vd:g}".replace(".", "p") + "V"


def vd_tag(vd: float) -> str:
    return f"{vd:g}".replace(".", "p")


def meas_path(device: str, vd: float) -> Path:
    dev = C.DEVICES[device]
    return C.MEAS_ROOT / dev["folder"] / f"{device}_VD_{vd_file_tag(vd)}.csv"


@dataclass(frozen=True)
class ForwardCurve:
    device: str
    vd: float
    vg: np.ndarray        # V, config.n_forward(device) points
    i_raw: np.ndarray     # A, signed drain current as measured
    source: str

    @property
    def i_abs(self) -> np.ndarray:
        return np.abs(self.i_raw)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame({"VG_V": self.vg, "ID_A": self.i_raw})


def forward_branch(raw: pd.DataFrame, path: Path | str = "") -> pd.DataFrame:
    vg = raw["VG_V"].to_numpy(float)
    d = np.diff(vg)
    rev = np.where(d < -C.VG_TOL_V)[0]
    if len(rev) and rev[0] == 0:
        raise ValueError(f"acquisition starts with decreasing VG: {path}")
    end = int(rev[0]) + 1 if len(rev) else len(raw)
    fw = raw.iloc[:end]
    if np.any(np.diff(fw["VG_V"].to_numpy(float)) <= 0):
        raise ValueError(f"forward branch not strictly increasing: {path}")
    return fw.reset_index(drop=True)


def load_forward(device: str, vd: float, path: Path | None = None) -> ForwardCurve:
    path = Path(path) if path else meas_path(device, vd)
    raw = pd.read_csv(path)
    vd_file = float(np.median(raw["VD_V"].to_numpy(float)))
    if abs(vd_file - vd) > 1e-6:
        raise ValueError(f"{path}: VD column {vd_file} != requested {vd}")
    fw = forward_branch(raw, path)
    keep = (fw["VG_V"] >= C.vg_start(device) - C.VG_TOL_V) & (fw["VG_V"] <= C.VG_STOP + C.VG_TOL_V)
    fw = fw[keep].reset_index(drop=True)
    vg = fw["VG_V"].to_numpy(float)
    i = fw["ID_drain_A"].to_numpy(float)
    check_grid(vg, path, device)
    return ForwardCurve(device=device, vd=vd, vg=vg, i_raw=i, source=str(path))


def check_grid(vg: np.ndarray, path: Path | str = "", device: str | None = None) -> None:
    start = C.vg_start(device) if device else C.VG_START
    n = C.n_forward(device) if device else C.N_FORWARD
    want = start + C.VG_STEP * np.arange(n)
    if len(vg) != n:
        raise ValueError(f"{path}: {len(vg)} forward points, expected {n}")
    if np.max(np.abs(vg - want)) > C.VG_TOL_V:
        raise ValueError(f"{path}: forward VG grid deviates by {np.max(np.abs(vg - want)):.4g} V")


def all_split_files(program: str = "main") -> list[tuple[str, str, float]]:
    return [(split, dev, vd) for split, items in C.splits(program).items() for dev, vd in items]
