from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]
ROOT = HARNESS.parent
RUNS_ROOT = ROOT / "runs"

# ---------------------------------------------------------------------------
# Device: devices/<name>/device.toml at the package root (devices/example by default, see its README.md)
# ---------------------------------------------------------------------------
DEVICE_DIR = Path(os.environ.get("TCADCAL_DEVICE", str(ROOT / "devices" / "example"))).resolve()
DEVICE_PROFILE = tomllib.loads((DEVICE_DIR / "device.toml").read_text())
_P = DEVICE_PROFILE

# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------
SENTAURUS_VERSION = "W-2024.09-SP1"
SENTAURUS_ROOT = Path(os.environ.get("SENTAURUS_ROOT", "/opt/sentaurus"))  # your Sentaurus installation
SENTAURUS_BIN = SENTAURUS_ROOT / "bin"
LICENSE_ENV = {k: os.environ[k] for k in ("SNPSLMD_LICENSE_FILE", "LM_LICENSE_FILE") if k in os.environ}
MANUAL_PDF = Path(os.environ.get("SDEVICE_MANUAL_PDF", str(SENTAURUS_ROOT / "sdevice_ug.pdf")))
# Sentaurus support files of the device (device.toml [materials] support_files), not distributed with this package.
SYNOPSYS_SUPPORT_DIR = Path(os.environ.get("TCAD_SUPPORT_DIR", str(HARNESS / "support")))
SYNOPSYS_SUPPORT_FILES = tuple(_P["materials"]["support_files"])

THREADS = int(os.environ.get("TCADCAL_THREADS", "4"))
CPU_SLOTS = int(os.environ.get("TCADCAL_SLOTS", "2"))
SNAPSHOT_WORKERS = int(os.environ.get("TCADCAL_SNAPSHOT_WORKERS", "4"))
SDEVICE_TIMEOUT_S = 3600
GLOBAL_SLOTS = int(os.environ.get("TCADCAL_GLOBAL_SLOTS", "4"))
GLOBAL_SLOT_DIR = Path(os.environ.get("TCADCAL_GLOBAL_SLOT_DIR", str(HARNESS.parent / "tmp" / ".host_slots")))
SDE_TIMEOUT_S = 600

MEAS_ROOT = ROOT / "experiment" / "raw"
# Devices (device.toml [devices]): folder under MEAS_ROOT (default: the device name), sample label, geometry and the
# optional vg_start and noise_vg_max.
DEVICES = {name: {"folder": d.get("folder", name), **{k: v for k, v in d.items() if k != "folder"}}
           for name, d in _P["devices"].items()}
# name: channel length (S/D inner-edge gap) and overlap per side, um (device.toml [geometries])
GEOMETRIES = {name: dict(g) for name, g in _P["geometries"].items()}
MARGIN_UM = 0.25


def _pairs(table: dict) -> dict:
    return {k: [(str(d), float(v)) for d, v in items] for k, items in table.items()}


# (device, V_D) of the calibration curves and of the tests without retuning (device.toml [splits])
SPLITS = _pairs(_P["splits"])
CALIBRATION = SPLITS["calibration"]
HELDOUT_SPLITS = [k for k in SPLITS if k != "calibration"]

# Recalibration example (device.toml [splits_recal]): the same roles for a second device.
SPLITS_RECAL = _pairs(_P["splits_recal"])
CAL_VDS = tuple(vd for _, vd in CALIBRATION)          # the two calibration drain biases, lower first
if len(CAL_VDS) != 2 or not CAL_VDS[0] < CAL_VDS[1]:
    raise ValueError(f"device.toml: splits.calibration needs two drain biases, the lower first (found {CAL_VDS})")
if tuple(vd for _, vd in SPLITS_RECAL.get("calibration", CALIBRATION)) != CAL_VDS:
    raise ValueError("device.toml: splits_recal.calibration must use the drain biases of splits.calibration")
VD_KEYS = tuple(f"{vd:g}" for vd in CAL_VDS)          # "0.05", "1": the V_D keys of the J terms and outputs
PROGRAMS = {"main": SPLITS, "recal": SPLITS_RECAL}


def splits(program: str = "main") -> dict:
    return PROGRAMS[program]


def calibration(program: str = "main") -> list:
    return PROGRAMS[program]["calibration"]

# Forward sweep of the measured curves (device.toml [sweep]).
VG_START, VG_STOP, VG_STEP = (float(_P["sweep"][k]) for k in ("vg_start", "vg_stop", "vg_step"))
MEAS_T_K = float(_P["sweep"].get("temperature_K", 300.0))   # measurement temperature, K
# Simulated sweep: wider than the measured one so that the gate work-function alignment
# (a rigid V_G shift of up to +-ALIGN_WINDOW_V) stays inside the simulated range.
ALIGN_WINDOW_V = 0.30
ALIGN_MAX_RECENTER = 3   # re-simulations when the best shift lies at the window edge
VG_SIM_START, VG_SIM_STOP = VG_START - ALIGN_WINDOW_V, VG_STOP + ALIGN_WINDOW_V
N_FORWARD = int(round((VG_STOP - VG_START) / VG_STEP)) + 1
VG_TOL_V = 1.0e-3  # tolerance on the sweep end points


def vg_start(device: str) -> float:
    return DEVICES[device].get("vg_start", VG_START)


def n_forward(device: str) -> int:
    return int(round((VG_STOP - vg_start(device)) / VG_STEP)) + 1

# Materials and region names of the structure (device.toml [materials], [regions]).
MATERIALS = dict(_P["materials"])
REGIONS = dict(_P["regions"])
CHANNEL, GATE_DIELECTRIC, PASSIVATION = MATERIALS["channel"], MATERIALS["gate_dielectric"], MATERIALS["passivation"]
CHANNEL_PAR = MATERIALS["channel_par_file"]
T_CH_KEY, K_CH_KEY, K_GD_KEY = f"t_{CHANNEL}_nm", f"k_{CHANNEL}", f"k_{GATE_DIELECTRIC}"
CH_MAT_KEY = f"{CHANNEL.lower()}_material"
# Device specification asserted on every mesh (tdx read-back) and every rendered deck (device.toml [structure]).
_S = _P["structure"]
DEVICE_SPEC = {
    T_CH_KEY: float(_S["t_channel_nm"]),
    "t_gox_nm": float(_S["t_gate_dielectric_nm"]),
    "t_pass_nm": float(_S["t_passivation_nm"]),
    "h_sd_nm": float(_S["h_contact_nm"]),
    "Lov_each_um": float(_S["Lov_each_um"]),
    "W_um": float(_S["W_um"]),
    "AreaFactor": float(_S["AreaFactor"]),
    K_GD_KEY: float(_S["k_gate_dielectric"]),
    K_CH_KEY: float(_S["k_channel"]),
}
PROBE_DEPTHS_UM = {k: float(v) for k, v in _S["probe_depths_um"].items()}
DECK_VARIABLES = dict(_P["deck_variables"])
GEOM_TOL_UM = 1.0e-3

# ---------------------------------------------------------------------------
# Objective J (tolerance-normalized least squares on the forward sweep).
# Set by the user before a calibration.
# ---------------------------------------------------------------------------
SIGMA_V = 0.050
SIGMA_R = 0.04           # dec, current tolerance (about 10 %)
NOISE_WINDOW_VG_MAX = float(_P["sweep"]["noise_vg_max"])  # V, forward points used for the off-state noise scale


def noise_vg_max(device: str) -> float:
    return DEVICES.get(device, {}).get("noise_vg_max", NOISE_WINDOW_VG_MAX)
LOQ_K = 10.0             # limit of quantification = 10 x robust noise scale
MAD_TO_SIGMA = 1.4826

VTH_I0_A = float(_P["metrics"]["vth_current_per_square_A"])


def vth_level_A(device: str) -> float:
    g = GEOMETRIES[DEVICES[device]["geometry"]]
    return VTH_I0_A * DEVICE_SPEC["W_um"] / g["L_um"]


VTH_LEVEL_A = float(_P["metrics"]["vth_level_A"])
SS_LOW_A, SS_HIGH_A = (float(x) for x in _P["metrics"]["ss_window_A"])
SS_LOW_BY_PROGRAM = {k: float(v) for k, v in _P["metrics"]["ss_low_A"].items()}


def ss_low(program: str = "main") -> float:
    return SS_LOW_BY_PROGRAM[program]
LOG_FLOOR_A = float(_P["metrics"]["log_floor_A"])
CURRENT_FLOOR_A = 1.0e-15
TOL = {"dVth_V": 0.050, "dSS_rel": 0.15, "dlogIon_dec": 0.050, "E_lin": 0.10}
MISSING_METRIC_RESIDUAL = 3.0   # a metric that cannot be extracted counts as 3 tolerances
OBJECTIVES = ("J_objective",)
TERM_OBJECTIVES = ("J_objective",)
# the terms of J (the V_th shift is reported, not a term, unless VTH_SHIFT_IN_J)
J_TERMS_ALL = (*(f"V_th@{v}" for v in VD_KEYS), "V_th_shift",
               *(f"{m}@{v}" for m in ("SS_profile", "I_on", "E_lin") for v in VD_KEYS),
               *(f"region:{r}@{v}" for r in ("subthreshold", "turn_on", "on") for v in VD_KEYS))
VTH_SHIFT_IN_J = False
J_TERMS = tuple(t for t in J_TERMS_ALL if t != "V_th_shift" or VTH_SHIFT_IN_J)          # the 14 terms of J
VG_ION_V = float(_P["metrics"]["ion_vg"])
POINT_OFF, POINT_ON = f"off (V_G {VG_START:.1f} V)", f"on (V_G {VG_ION_V:.1f} V)"   # bias-point labels of the diagnostics
POINT_VTH = f"threshold ({VTH_LEVEL_A * 1e9:g} nA)"   # the V_th point, the same label in inside.py and bands.py
ALIGNMENT_LOSSES = (None, "full_objective")
GATE_FREE_LOSSES = ("full_objective",)
FULL_OBJECTIVE_WF_BASIS = ("one common effective gate offset for both calibration drain biases, set after every simulation "
                           "by minimizing the full objective J_objective over the gate offset; no interval")
GATE_WF_REFERENCE = dict(_P["gate_wf_reference"])
# Gate capacitance for the illustrative charge equivalence of the gate offset (gate dielectric of device.toml, charge
# assumed at its interface with the channel). Illustrative only: not an extracted defect density or an identified mechanism.
C_OX_F_CM2 = DEVICE_SPEC[K_GD_KEY] * 8.8541878128e-14 / (DEVICE_SPEC["t_gox_nm"] / 1e7)
SS_PROFILE = {"window_dec": 1.0, "step_dec": 0.5, "min_points": 2, "tol_rel": 0.15}
SS_WINDOWS_FROZEN = HARNESS / "lib" / "tcadcal" / "data" / "ss_windows.json"
SS_WINDOWS_FROZEN_BY_PROGRAM = {"main": SS_WINDOWS_FROZEN, "recal": HARNESS / "lib" / "tcadcal" / "data" / "ss_windows_recal.json"}
TOL_VTH_SHIFT_REL = 0.15     # V_th-shift tolerance, fraction of the measured shift (as for SS)
TOL_VTH_SHIFT_MIN_V = 1.0e-3  # numerical floor only
CURVE_REGIONS = ("subthreshold", "turn_on", "on")

ACCEPT_MIN_DJ = 1.0e-3
STALL_REL_GAIN = 0.02
STALL_TREND_N = 10
STALL_NEAR_FACTOR = 3.0
STALL_MIN_POINTS = 6
STALL_TRADEOFF_CORR = -0.5
STALL_MOVABLE_TOL = 0.1
ACTIVATION_MIN_J = 1.0     # J above the tolerance level
ACTIVATION_RUNS_P = 0.01
DELTA_P = float(os.environ.get("TCADCAL_DELTA_P", "0.10"))   # trust radius in scaled [0,1] units
RHO_END = 0.02
DFOLS_MAXFUN_BASE = 6     # per call: n + 1 + DFOLS_MAXFUN_PER_PARAM * n + DFOLS_MAXFUN_BASE
DFOLS_MAXFUN_PER_PARAM = 4
DFOLS_MAX_CALLS = 3       # repeated calls per job before the stall is declared
SAFEGUARD_STEP = 0.20

RECOVERY_LEVELS = [
    {"name": "nominal", "MAXSTEP": "0.05", "INITSTEP": "1e-2", "MINSTEP": "1e-9", "MINSTEP_D": "1e-8", "ITER": "200",
     "DIGITS": "5", "ERRREF": "1e10"},
    {"name": "bias_step", "MAXSTEP": "0.01", "INITSTEP": "1e-3", "MINSTEP": "1e-11", "MINSTEP_D": "1e-10", "ITER": "200",
     "DIGITS": "5", "ERRREF": "1e10"},
    {"name": "iterations", "MAXSTEP": "0.01", "INITSTEP": "1e-3", "MINSTEP": "1e-11", "MINSTEP_D": "1e-10", "ITER": "400",
     "DIGITS": "5", "ERRREF": "1e10"},
    {"name": "tolerance", "MAXSTEP": "0.01", "INITSTEP": "1e-3", "MINSTEP": "1e-11", "MINSTEP_D": "1e-10", "ITER": "400",
     "DIGITS": "4", "ERRREF": "1e10"},
    {"name": "mesh_refine", "MAXSTEP": "0.05", "INITSTEP": "1e-2", "MINSTEP": "1e-9", "MINSTEP_D": "1e-8", "ITER": "200",
     "DIGITS": "5", "ERRREF": "1e10", "MESH_SCALE": 0.5, "TIMEOUT_FACTOR": 2},
]
AGENT_TOOLS = ("kb_search", "evaluate", "ofat", "dfols", "deck_patch", "state", "jobs",
               "diagnose", "extract", "identify", "vd_consistency", "verify")
CALIBRATION_RECOVERY_LEVELS = [0, 1]
# Optional: frozen mesh hash per "<geometry>|<mesh scale>" of your device; empty = no check.
FROZEN_MESH_HASHES: dict[str, str] = {}
# Refinement definitions of the start structure deck that the mesh_refine level scales. The 20 nm background
# window is kept.
MESH_REFINE_DEFS = ("RD_ch", "RD_ox", "RD_edge")

# Mesh refinement specs of the structure deck (device.toml [mesh.specs]).
MESH_SPECS = {name: {k: tuple(v) if isinstance(v, list) else v for k, v in spec.items()}
              for name, spec in _P["mesh"]["specs"].items()}
VERIFY_CRITERIA = {"Vth_mV": 5.0, "Vth_shift_mV": 1.5, "SS_pct": 1.5, "logIon_dec": 0.005, "E_lin": 0.01, "point_dec": 0.004}
BALANCE_FLOOR_A = float(_P["metrics"]["balance_floor_A"])      # |I_D| below the measured detection limit does not enter J
BALANCE_MAX_DEC = 0.004        # terminal-current imbalance above 1/10 of the 0.04 dec point tolerance = false convergence
SENSITIVITY_FRACTION = 0.1     # LOW_SENSITIVITY: every J term moves by less than this many tolerances per probe step
ENVELOPE_LOG_DEC = 0.5         # verified neighbourhood of a card: densities within 0.5 dec,
ENVELOPE_EV = 0.05             # energies within 0.05 eV,
ENVELOPE_REL = 0.25            # other parameters within 25 %
BUDGET_CATEGORY_CAPS = {"diagnostic": 40}
# Start-deck parameter cards whose numerics you have checked on your production mesh before a run, as
# [{"eval": "<label>", "values": {"WF_sd": ..., "Nd_eff": ..., "mu0": ...}}, ...]. The flag for values outside the
# numerically verified range uses them. None: every start-deck evaluation carries the flag.
V000_VERIFIED_SEED: list[dict] | None = None
SILICON_TEMPLATE_SECTIONS = {
    r"\bOldSlotboom\b": "OldSlotboom", r"\bSlotboom\b": "Slotboom", r"\bdelAlamo\b": "delAlamo",
    r"\bBennett\b": "Bennett", r"\bJainRoulston\b": "JainRoulston",
    r"\b[eh]QuantumPotential\b|\bDensityGradient\b": "QuantumPotentialParameters",
    r"\bMLDA\b": "MLDAQMModel", r"\bVanDort\b": "VanDortQMModel",
    r"\bEnormal\b": "EnormalDependence", r"\bCarrierCarrierScattering\b": "CarrierCarrierScattering",
    r"\bPooleFrenkel\b": "PooleFrenkel", r"\bBand2Band\b|\bBand2BandTunneling\b": "Band2BandTunneling",
    r"\bBarrierLowering\b": "BarrierLowering", r"\bDirectTunnel": "DirectTunnelling",
    r"\bHighFieldSaturation\b|\bHighFieldDependence\b": "HighFieldDependence",
}
PHYSICS_STALL_MIN_VALID = 0.9   # a fit stall opens a physics-model addition only with >= 90 % converged points
DIAG_SNAP_SPACING = 0.0208     # on-request diagnostic run: snapshots every 2nd current-plot grid point (75 mV) over the whole sweep
# Append the harness-owned CurrentPlot section and trap / tunneling Plot fields at rendering (deck.add_harness_outputs).
HARNESS_OUTPUTS = True
MESH_PRODUCTION: str | None = _P["mesh"].get("production")
# Resolution spec asserted on every mesh (mesh.assert_resolution); set from the production mesh.
MESH_RESOLUTION_MIN: dict | None = None
TRAPDLN_START_LINE = "   TrapDLN          = 1000"
TRAPDLN_PEAK_SAMPLING: int | None = 100

# Web queries and pages the agent may not use: add the identifiers of your own publications on the device you
# calibrate (preprint numbers, titles, author names), so that the agent cannot copy a published calibration.
WEB_BLOCK_TERMS: list[str] = []
WEB_BLOCK_COMBOS: list[tuple[str, str]] = []   # pairs of words that block a query when both occur


# ---------------------------------------------------------------------------
# Device wording of the agent instructions: templates/ and skills/ hold [[NAME]] places that make_workspace.py fills
# from this table (device.toml [text] and the values above).
# ---------------------------------------------------------------------------
def _v1(v: float) -> str:
    """Voltage in the instruction texts: one decimal for whole values (1.0, -1.0), else the short form (0.05)."""
    return f"{v:.1f}" if float(v).is_integer() else f"{v:g}"


_G0 = GEOMETRIES[DEVICES[CALIBRATION[0][0]]["geometry"]]
TEXT = {
    **{k.upper(): str(v) for k, v in _P["text"].items()},
    "CHANNEL": CHANNEL, "GATE_DIELECTRIC": GATE_DIELECTRIC, "GATE_METAL": MATERIALS["gate"], "CHANNEL_PAR": CHANNEL_PAR,
    "K_CHANNEL": f"{DEVICE_SPEC[K_CH_KEY]:g}", "K_GATE_DIELECTRIC": f"{DEVICE_SPEC[K_GD_KEY]:g}",
    "AREA_FACTOR": f"{DEVICE_SPEC['AreaFactor']:g}", "W_UM": f"{DEVICE_SPEC['W_um']:g}", "L_UM": f"{_G0['L_um']:g}",
    "VD_LO": _v1(CAL_VDS[0]), "VD_HI": _v1(CAL_VDS[1]), "VD_LO_G": VD_KEYS[0], "VD_HI_G": VD_KEYS[1],
    "VG_START": _v1(VG_START), "VG_STOP": _v1(VG_STOP), "VG_START_G": f"{VG_START:g}", "VG_STOP_G": f"{VG_STOP:g}",
    "VG_SIM_START": _v1(round(VG_SIM_START, 6)), "VG_SIM_STOP": _v1(round(VG_SIM_STOP, 6)),
    "N_FORWARD": str(N_FORWARD), "NOISE_VG_MAX": _v1(NOISE_WINDOW_VG_MAX),
    "VTH_LEVEL": f"{VTH_LEVEL_A * 1e9:g} nA", "VTH_PER_SQUARE": f"{VTH_I0_A * 1e9:g} nA", "ION_VG_G": f"{VG_ION_V:g}",
    "TEMPERATURE": f"{MEAS_T_K:g}",
}
vtext = _v1
_TOKEN = re.compile(r"\[\[([A-Z0-9_]+)\]\]")


def render_text(text: str) -> str:
    """Fill the [[NAME]] places of an instruction text from TEXT (an unknown name is an error)."""
    return _TOKEN.sub(lambda m: TEXT[m.group(1)], text)
