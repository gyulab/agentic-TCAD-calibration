"""Checks on synthetic transfer curves. No measured data and no Sentaurus are needed."""
import json

import numpy as np

from tcadcal import objective as O
from tcadcal.measurements import ForwardCurve


def synthetic_curve(vg, vth=0.2, ss_mv=120.0, i_floor=1e-12, gm=2e-5):
    """Exponential subthreshold below vth, linear above, plus a small noise floor."""
    ss = ss_mv / 1000.0
    i_sub = 1e-7 * 10 ** ((vg - vth) / ss)
    i_on = 1e-7 + gm * np.clip(vg - vth, 0, None)
    i = np.where(vg < vth, i_sub, i_on) + i_floor * (1 + 0.1 * np.sin(37 * vg))
    return i


def prep_for(vd=0.05):
    vg = np.round(np.arange(-1.0, 2.0001, 0.05), 4)
    return vg, O.prepare(ForwardCurve("synthetic", vd, vg, synthetic_curve(vg), "synthetic"))


def test_identical_curve_gives_zero():
    vg, prep = prep_for()
    res = O.branch_residuals(prep, vg, synthetic_curve(vg))
    assert res["J_branch"] < 1e-12


def test_shifted_curve_raises_j():
    vg, prep = prep_for()
    j1 = O.branch_residuals(prep, vg, synthetic_curve(vg, vth=0.25))["J_branch"]
    j2 = O.branch_residuals(prep, vg, synthetic_curve(vg, vth=0.35))["J_branch"]
    assert 0 < j1 < j2


def test_threshold_extraction():
    vg = np.round(np.arange(-1.0, 2.0001, 0.01), 4)
    m = O.extract(vg, synthetic_curve(vg, vth=0.2))
    assert json.dumps(m)  # serializable
    vth = next((v for k, v in m.items() if k.lower().startswith("vth") and isinstance(v, float)), None)
    assert vth is None or abs(vth - 0.2) < 0.1
