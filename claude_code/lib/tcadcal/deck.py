"""Deck templates, rendering and static guards.

  * structure and dimensions: the SDE template is byte-identical to deck0, one AreaFactor (the width, device.toml);
  * permittivities: the gate-dielectric Epsilon = Epsilon_aniso of device.toml, no other epsilon (the channel material
    file holds the channel value);
  * measurement conditions: the File section, the electrode set and their zero start bias, the bias sweep of
    the Solve section, and the measurement temperature (device.toml) wherever a temperature is set;
  * solver settings: the deck0 Math entries and the Solve steps. A Math entry may be added only as a
    declaration tied to a location of the structure, and a Coupled equation list may gain only the equations
    that a Physics model needs;
  * the gate electrode entry (its work function is set by the alignment);
  * no PMI / user library / include / external load, no ferroelectric polarization.
Every rendered deck is checked by ``guard_rendered`` before Sentaurus is called (tokens filled, rendered values
equal the requested ones, geometry, sweep and epsilon read back).
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import config as C
from .util import fmt_value, md5_file, sha256_text

FILES = {"sde": "sde_dvs.cmd", "sdevice": "sdevice_idvg.cmd", "par": "sdevice_iwo_process.par"}
DECK0 = C.DEVICE_DIR / "deck0"
GEOMETRY_TOKENS = {"L_um", "Lov_total_um", "Lov_each_um", "x_total_right", "x_source_inner", "x_drain_inner",
                   "x_drain_outer", "VD", "THREADS", "INITSTEP", "MINSTEP", "MINSTEP_D", "MAXSTEP", "ITER", "DIGITS",
                   "ERRREF"}
PROTECTED_SDEVICE_SECTIONS = ("File", "Electrode", "Math", "Solve")   # checked by check_template, not free text
SDEVICE_SECTIONS = ("File", "Electrode", "Thermode", "Physics", "Math", "Solve")   # plus any "*Plot" section
# Equations a Physics model may add to a Coupled list (Sentaurus Device: lattice and carrier temperatures,
# quantum potentials). The bias steps around them stay fixed.
EXTRA_EQUATIONS = ("Temperature", "eTemperature", "hTemperature", "eQuantumPotential", "hQuantumPotential")
LOCATION_RE = re.compile(r"(?i)\b(Electrode|Material|Region|MaterialInterface|RegionInterface)\s*=")
PAR_BLOCK_RE = re.compile(r'(Material|Region|MaterialInterface|RegionInterface|Electrode)\s*=\s*"([^"]+)"\s*\{')
FORBIDDEN_PATTERNS = [
    (r"\bPMI", "PMI / user model library"), (r"\.so\b", "shared library"), (r"Polarization", "ferroelectric polarization"),
    (r"Ferro", "ferroelectric model"), (r"#\s*include", "include directive"), (r"\bLoad\b", "external load"),
    (r"\bSystem\s*\(", "system call"), (r"\bGrid\s*=", "grid redefinition"),
]
TOKEN_RE = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)@")


class DeckGuardError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("*") or s.startswith(";"):
            continue
        line = re.sub(r"\s#.*$", "", line)          # trailing '#' comments
        line = re.sub(r"\s\*\s.*$", "", line)       # trailing ' * ' comments (par style)
        out.append(line.rstrip())
    return "\n".join(out)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", strip_comments(text)).strip()


def top_level_blocks(text: str) -> list[dict[str, Any]]:
    """Top-level ``Name (qualifier) { ... }`` blocks of a sdevice command file (comments removed)."""
    src = strip_comments(text)
    blocks, i, n = [], 0, len(src)
    head_re = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\([^{}]*\))?\s*\{")
    while i < n:
        m = head_re.match(src, i)
        if not m:
            j = src.find("\n", i)
            if j < 0:
                break
            if src[i:j].strip():
                raise DeckGuardError(f"unparsed text at top level: {src[i:j].strip()[:80]!r}")
            i = j + 1
            continue
        depth, k = 1, m.end()
        while k < n and depth:
            depth += {"{": 1, "}": -1}.get(src[k], 0)
            k += 1
        if depth:
            raise DeckGuardError(f"unbalanced braces in block {m.group(1)}")
        blocks.append({"name": m.group(1), "qual": (m.group(2) or "").strip(), "body": src[m.end():k - 1]})
        i = k
    return blocks


def par_material_blocks(text: str) -> dict[str, str]:
    src = strip_comments(text)
    out, pos = {}, 0
    for m in re.finditer(r'Material\s*=\s*"([^"]+)"\s*\{', src):
        if m.start() < pos:
            continue
        depth, k = 1, m.end()
        while k < len(src) and depth:
            depth += {"{": 1, "}": -1}.get(src[k], 0)
            k += 1
        if m.group(1) in out:
            raise DeckGuardError(f"duplicate Material block {m.group(1)}")
        out[m.group(1)] = src[m.end():k - 1]
        pos = k
    return out


def tokens_in(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text))


# ---------------------------------------------------------------------------
# Deck versions
# ---------------------------------------------------------------------------
def read_templates(deck_dir: Path) -> dict[str, str]:
    return {k: (Path(deck_dir) / f).read_text() for k, f in FILES.items()}


def deck_hash(deck_dir: Path) -> str:
    t = read_templates(deck_dir)
    return sha256_text("\n\x00".join(t[k] for k in sorted(t)))[:16]


def _top_items(body: str) -> list[str]:
    """Entries of a section body, split at newlines outside (), {} and []."""
    items, depth, cur = [], 0, []
    for ch in body:
        depth += {"(": 1, "{": 1, "[": 1, ")": -1, "}": -1, "]": -1}.get(ch, 0)
        if ch == "\n" and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur))
    return [re.sub(r"\s+", " ", i).strip() for i in items if i.strip()]


def _brace_items(body: str) -> list[str]:
    """The ``{ ... }`` entries of an Electrode or Thermode section."""
    out, depth, start = [], 0, None
    for k, ch in enumerate(body):
        if ch == "{":
            if depth == 0:
                start = k + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                out.append(re.sub(r"\s+", " ", body[start:k]).strip())
    return out


def _strip_extra_equations(solve: str) -> str:
    """Solve text with the allowed extra equations removed from every Coupled list."""
    def sub(m: re.Match) -> str:
        keep = [w for w in m.group(2).split() if w not in EXTRA_EQUATIONS]
        return f"Coupled {m.group(1) or ''} {{ {' '.join(keep)} }}"
    return re.sub(r"Coupled\s*(\([^()]*\))?\s*\{([^{}]*)\}", sub, solve)


def par_blocks(text: str) -> tuple[list[tuple[str, str, str]], str]:
    """Top-level blocks of a parameter file: (kind, name, body), and any text outside them."""
    src = strip_comments(text)
    out, pos, rest = [], 0, []
    for m in PAR_BLOCK_RE.finditer(src):
        if m.start() < pos:
            continue
        rest.append(src[pos:m.start()])
        depth, k = 1, m.end()
        while k < len(src) and depth:
            depth += {"{": 1, "}": -1}.get(src[k], 0)
            k += 1
        out.append((m.group(1), m.group(2), src[m.end():k - 1]))
        pos = k
    rest.append(src[pos:])
    return out, "".join(rest).strip()


def _epsilon_blocks(body: str) -> list[str]:
    return sorted(_norm(m.group(0)) for m in re.finditer(r"Epsilon(?:_aniso)?\s*\{[^{}]*\}", body))


def check_template(templates: Mapping[str, str], registry_tokens: set[str]) -> list[str]:
    """Static checks of a template set (before rendering): only the fixed items of the model space are enforced."""
    errs: list[str] = []
    base = read_templates(DECK0)
    if templates["sde"] != base["sde"]:
        errs.append("SDE structure template differs from deck0 (structure and dimensions are fixed)")
    try:
        blocks = top_level_blocks(templates["sdevice"])
        base_blocks = top_level_blocks(base["sdevice"])
    except DeckGuardError as exc:
        return errs + [f"sdevice parse: {exc}"]
    one = {}
    for sec in ("File", "Electrode", "Math", "Solve"):
        got = [b for b in blocks if b["name"] == sec]
        if len(got) != 1 or got[0]["qual"]:
            errs.append(f"exactly one unqualified {sec} section is required")
        else:
            one[sec] = got[0]["body"]
    bb = {b["name"]: b["body"] for b in base_blocks}
    for b in blocks:
        if b["name"] == "CurrentPlot":
            errs.append("the CurrentPlot section is written by the harness (internal-quantity outputs); the deck may not "
                        "define one")
            continue
        if b["name"] not in SDEVICE_SECTIONS and not b["name"].endswith("Plot"):
            errs.append(f"top-level section {b['name']} is not allowed: the deck stays a single-device simulation "
                        "(no circuit or mixed-mode sections)")
    # measurement conditions: files and bias sweep
    if "File" in one and _norm(one["File"]) != _norm(bb["File"]):
        errs.append("File section changed (fixed)")
    if "Solve" in one:
        if _norm(_strip_extra_equations(one["Solve"])) != _norm(_strip_extra_equations(bb["Solve"])):
            errs.append("Solve section changed: the bias sweep and solver steps are fixed. A Coupled equation list may "
                        "only gain the equations that a Physics model in the deck needs")
    # electrodes: the same three contacts at zero start bias, the gate entry unchanged (alignment)
    if "Electrode" in one:
        items, base_items = _brace_items(one["Electrode"]), _brace_items(bb["Electrode"])
        names = [re.search(r'Name\s*=\s*"([^"]+)"', i).group(1) if re.search(r'Name\s*=\s*"([^"]+)"', i) else None
                 for i in items]
        base_names = [re.search(r'Name\s*=\s*"([^"]+)"', i).group(1) for i in base_items]
        if sorted(n or "" for n in names) != sorted(base_names):
            errs.append(f"Electrode section must hold exactly the contacts {base_names}, found {names}")
        for it, n in zip(items, names):
            v = re.findall(r"\bVoltage\s*=\s*([-+0-9.eE]+)", it)
            if len(v) != 1 or float(v[0]) != 0.0:
                errs.append(f"electrode {n}: the start bias is fixed at Voltage=0 (the sweep is applied in Solve)")
            if re.search(r"(?i)\b(Current|Charge)\s*=", it):
                errs.append(f"electrode {n}: current or charge boundary conditions change the measurement setup")
        gate = [i for i, n in zip(items, names) if n == "gate"]
        bgate = [i for i, n in zip(base_items, base_names) if n == "gate"]
        if len(gate) != 1 or gate[0] != bgate[0]:
            errs.append("the gate electrode entry is fixed (its work function is set by the alignment)")
    # solver settings: deck0 Math entries in order and unchanged; additions are location-tied declarations
    if "Math" in one:
        got, want = _top_items(one["Math"]), _top_items(bb["Math"])
        keys = {re.match(r"[A-Za-z_]+", w).group(0).lower() for w in want}
        k = 0
        for it in got:
            if k < len(want) and it == want[k]:
                k += 1
                continue
            head = (re.match(r"[A-Za-z_]+", it) or re.match(r"", it)).group(0).lower()
            if head in keys or not LOCATION_RE.search(it) or "(" not in it:
                errs.append(f"Math entry {it[:80]!r} is not allowed: the solver settings of deck0 are fixed. An added Math "
                            "entry may only be a declaration that the manual requires for a Physics model, tied to a "
                            "location of this structure (Electrode=, Region=, Material=, RegionInterface= or "
                            "MaterialInterface=)")
        if k != len(want):
            errs.append(f"Math section must keep the deck0 solver settings unchanged and in order (missing or changed: "
                        f"{want[k][:60]!r})")
    # temperature, width, permittivity
    glob = [b for b in blocks if b["name"] == "Physics" and not b["qual"]]
    if len(glob) != 1:
        errs.append("exactly one unqualified Physics block is required")
    sdev_nc = strip_comments(templates["sdevice"])
    af, tk = f"{C.DEVICE_SPEC['AreaFactor']:g}", f"{C.MEAS_T_K:g}"
    if len(re.findall(r"AreaFactor", sdev_nc)) != 1 or not re.search(rf"AreaFactor\s*=\s*{re.escape(af)}\b", sdev_nc):
        errs.append(f"AreaFactor must appear once, equal to {af} (channel width)")
    if not re.search(rf"Temperature\s*=\s*{re.escape(tk)}\b", glob[0]["body"] if glob else ""):
        errs.append(f"Temperature = {tk} missing from the global Physics block")
    temps = re.findall(r"\bTemperature\s*=\s*([-+0-9.eE]+)", sdev_nc)
    if any(float(x) != C.MEAS_T_K for x in temps):
        errs.append(f"every temperature setting must be {tk} K (measurement temperature), found {temps}")
    if re.search(r"(?i)epsilon", sdev_nc):
        errs.append("epsilon may not be set in the device deck (permittivities are fixed)")
    if re.search(r"Workfunction", "\n".join(b["qual"] + " " + b["body"] for b in blocks if b["name"] != "Electrode")):
        errs.append("work functions may only be set in the Electrode section")
    # parameter file: permittivities fixed, the channel material-file insert kept
    ch, gd, cpar = C.CHANNEL, C.GATE_DIELECTRIC, C.CHANNEL_PAR
    mats_want = sorted({ch, gd, C.PASSIVATION})
    pblocks, rest = par_blocks(templates["par"])
    base_pb, _ = par_blocks(base["par"])
    if rest:
        errs.append(f"parameter file: text outside the parameter blocks: {rest[:80]!r}")
    mats = [n for k, n, _ in pblocks if k == "Material"]
    if sorted(set(mats)) != mats_want or len(mats) != len(mats_want):
        errs.append(f"parameter file must hold one Material block each for {' and '.join(mats_want[::-1])} (the materials "
                    f"of this structure), found {mats}")
    seen = [(k, n) for k, n, _ in pblocks]
    if len(seen) != len(set(seen)):
        errs.append("parameter file: duplicate blocks")
    body = {(k, n): b for k, n, b in pblocks}
    base_body = {(k, n): b for k, n, b in base_pb}
    hf = body.get(("Material", gd))
    if hf is not None and _epsilon_blocks(hf) != _epsilon_blocks(base_body[("Material", gd)]):
        errs.append(f"{gd} permittivity changed (Epsilon and Epsilon_aniso {C.DEVICE_SPEC[C.K_GD_KEY]:g} are fixed device inputs)")
    iwo = body.get(("Material", ch))
    if iwo is not None:
        if not re.search(rf'insert\s*=\s*"\./{re.escape(cpar)}"', iwo):
            errs.append(f'{ch} block must keep insert = "./{cpar}"')
        if re.search(r"(?i)epsilon", iwo):
            errs.append(f"{ch} epsilon override not allowed ({C.K_CH_KEY} = {C.DEVICE_SPEC[C.K_CH_KEY]:g} from {cpar})")
    for (k, n), b in body.items():
        if (k, n) != ("Material", gd) and (k, n) != ("Material", ch) and re.search(r"(?i)epsilon", b):
            errs.append(f"parameter block {k}={n}: epsilon may not be set (permittivities are fixed)")
    par_nc = strip_comments(templates["par"])
    if len(re.findall(r"(?i)\binsert\b", par_nc)) != 1:
        errs.append(f"only the {cpar} insert is allowed")
    free = {"sdevice": "\n".join(b["qual"] + " " + b["body"] for b in blocks if b["name"] not in ("File", "Solve")),
            "par": par_nc}
    for key, nc in free.items():
        for pat, what in FORBIDDEN_PATTERNS:
            if re.search(pat, nc):
                errs.append(f"{key}: forbidden construct ({what}): /{pat}/")
    used = set().union(*(tokens_in(strip_comments(t)) for t in templates.values()))
    unknown = used - GEOMETRY_TOKENS - registry_tokens
    if unknown:
        errs.append(f"unregistered tokens: {sorted(unknown)}")
    missing = registry_tokens - used
    if missing:
        errs.append(f"registered parameters not used in the deck: {sorted(missing)}")
    return errs


def check_fixed_tokens(templates: Mapping[str, str], fixed: Mapping[str, float]) -> list[str]:
    """Run rule fixed_work_functions: the source/drain contacts keep their work-function token."""
    if "WF_sd" not in fixed:
        return []
    errs = []
    try:
        el = next(b["body"] for b in top_level_blocks(templates["sdevice"]) if b["name"] == "Electrode")
    except (DeckGuardError, StopIteration):
        return ["Electrode section not found"]
    for it in _brace_items(el):
        name = re.search(r'Name\s*=\s*"([^"]+)"', it)
        if name and name.group(1) in ("source", "drain") and "Workfunction=@WF_sd@" not in it.replace(" ", ""):
            errs.append(f"electrode {name.group(1)}: Workfunction=@WF_sd@ is fixed in this run (run rule fixed_work_functions)")
    return errs


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def geometry_tokens(geometry: str) -> dict[str, str]:
    g = C.GEOMETRIES[geometry]
    L, lov = g["L_um"], g["Lov_each_um"]

    def f(v: float) -> str:
        return str(int(round(v))) if abs(v - round(v)) < 1e-9 else f"{v:.6g}"
    return {"L_um": f(L), "Lov_total_um": f(2 * lov), "Lov_each_um": f(lov),
            "x_total_right": f(2 * lov + L + C.MARGIN_UM), "x_source_inner": f(lov),
            "x_drain_inner": f(lov + L), "x_drain_outer": f(2 * lov + L)}


def solver_tokens(level: int, threads: int) -> dict[str, str]:
    lv = C.RECOVERY_LEVELS[level]
    return {"THREADS": str(int(threads)), "INITSTEP": lv["INITSTEP"], "MINSTEP": lv["MINSTEP"],
            "MINSTEP_D": lv.get("MINSTEP_D", "1e-8" if level < 2 else "1e-10"), "MAXSTEP": lv["MAXSTEP"], "ITER": lv["ITER"],
            "DIGITS": lv["DIGITS"], "ERRREF": lv["ERRREF"]}


def refine_mesh(sde: str, scale: float) -> str:
    hb = re.search(re.escape(MESH_BLOCK_BEGIN) + r" \(spec (\w+), scale ([0-9.eE+-]+)\)", sde)
    if hb:
        return with_mesh_block(sde, mesh_block(C.MESH_SPECS[hb.group(1)], float(hb.group(2)) * scale, hb.group(1)))
    found = []

    def sub(m: re.Match) -> str:
        found.append(m.group(2))
        sizes = " ".join(f"{float(v) * scale:.6g}" for v in m.group(3).split())
        return f'{m.group(1)}{sizes})'
    out = re.sub(r'^(\(sdedr:define-refinement-size "(' + "|".join(C.MESH_REFINE_DEFS) + r')" )([-+0-9.eE ]+)\)',
                 sub, sde, flags=re.M)
    if sorted(found) != sorted(C.MESH_REFINE_DEFS):
        raise DeckGuardError(f"mesh refine: refinement definitions {sorted(found)} != {sorted(C.MESH_REFINE_DEFS)}")
    return out


MESH_BLOCK_BEGIN = "; >>> harness mesh block"
MESH_BLOCK_END = "; <<< harness mesh block"


def mesh_block(spec: Mapping[str, Any], scale: float = 1.0, name: str = "") -> str:
    """SDE refinement block of a graded mesh spec (config.MESH_SPECS). ``scale`` multiplies every spacing (0.5 = the
    all-halved member of the family); grading ratios and window geometry are unchanged."""
    f = lambda v: f"{v * scale:.6g}"  # noqa: E731
    pair = lambda t: f"{f(t[0])} {f(t[1])}"  # noqa: E731
    hw = spec["edge_half_width"]
    lines = [MESH_BLOCK_BEGIN + f" (spec {name or '?'}, scale {scale:g})",
             '(sdedr:define-refeval-window "RW_bg" "Rectangle"',
             '  (position x_source_outer (+ y_pass_contact_top 5e-3) 0) (position x_drain_outer (- y_gate_bot 5e-3) 0))',
             f'(sdedr:define-refinement-size "RD_bg" {pair(spec["bg_max"])} {pair(spec["bg_min"])})',
             '(sdedr:define-refinement-placement "RP_bg" "RD_bg" (list "window" "RW_bg"))',
             f'(sdedr:define-refinement-size "RD_ch" {pair(spec["ch_max"])} {pair(spec["ch_min"])})',
             f'(sdedr:define-refinement-function "RD_ch" "MaxLenInt" "{C.CHANNEL}" "{C.GATE_DIELECTRIC}" {f(spec["ch_first"])} {spec["ch_ratio"]:g})',
             f'(sdedr:define-refinement-function "RD_ch" "MaxLenInt" "{C.CHANNEL}" "Contact" {f(spec["ch_first"])} {spec["ch_ratio"]:g})',
             f'(sdedr:define-refinement-region "RP_ch" "RD_ch" "{C.REGIONS["channel"]}")',
             f'(sdedr:define-refinement-size "RD_ox" {pair(spec["ox_max"])} {pair(spec["ox_min"])})',
             f'(sdedr:define-refinement-region "RP_ox" "RD_ox" "{C.REGIONS["gate_dielectric"]}")']
    for tag, xe in (("s", "x_source_inner"), ("d", "x_drain_inner")):
        for side, x0, x1, sign in (("L", f"(- {xe} {hw:g})", xe, -1), ("R", xe, f"(+ {xe} {hw:g})", 1)):
            lines += [f'(sdedr:define-refeval-window "RW_{tag}{side}" "Rectangle"',
                      f'  (position {x0} (- y_gox_bot 0.001) 0) (position {x1} (+ y_ch_top 0.006) 0))',
                      f'(sdedr:define-multibox-size "MB_{tag}{side}" {f(spec["edge_max_x"])} {f(spec["ch_max"][1])} '
                      f'{f(spec["edge_min_x"])} {f(spec["ch_min"][1])} {sign * spec["edge_ratio"]:g} 0)',
                      f'(sdedr:define-multibox-placement "MP_{tag}{side}" "MB_{tag}{side}" "RW_{tag}{side}")']
    lines.append(MESH_BLOCK_END)
    return "\n".join(lines) + "\n"


def with_mesh_block(sde: str, block: str) -> str:
    """Replace the mesh section of an SDE text (everything between the doping placement and build-mesh, or an
    earlier harness block) by ``block``. Geometry, contacts and doping are untouched."""
    key = '(sde:build-mesh "n1_msh")'
    if sde.count(key) != 1:
        raise DeckGuardError("SDE text must call build-mesh exactly once")
    if MESH_BLOCK_BEGIN in sde:
        a = sde.index(MESH_BLOCK_BEGIN)
        b = sde.index(MESH_BLOCK_END) + len(MESH_BLOCK_END) + 1
        return sde[:a] + block + sde[b:]
    marker = f'(sdepe:doping-constant-placement "Channel" n_type_dop Nd_eff "{C.REGIONS["channel"]}")\n'
    if sde.count(marker) != 1:
        raise DeckGuardError("SDE doping placement line not found")
    a = sde.index(marker) + len(marker)
    b = sde.index(key)
    return sde[:a] + "\n" + block + "\n" + sde[b:]


IWO_T_UM = C.DEVICE_SPEC[C.T_CH_KEY] / 1000.0   # channel film thickness, um
PROBE_DEPTHS = dict(C.PROBE_DEPTHS_UM)   # um above the gate interface, inside the channel film


def probe_points(device: str) -> dict[str, tuple[float, float]]:
    """Named probe points (x, y) in um: channel centre at three depths, 10 nm inside each inner contact edge (gate side
    and mid film), and 10 nm under each contact (mid film)."""
    g = C.GEOMETRIES[C.DEVICES[device]["geometry"]]
    xsi, xdi = g["Lov_each_um"], g["Lov_each_um"] + g["L_um"]
    xc = 0.5 * (xsi + xdi)
    pts = {f"centre_{k}": (xc, y) for k, y in PROBE_DEPTHS.items()}
    pts.update({"source_in_gate": (xsi + 0.01, PROBE_DEPTHS["gate"]), "source_in_mid": (xsi + 0.01, PROBE_DEPTHS["mid"]),
                "drain_in_gate": (xdi - 0.01, PROBE_DEPTHS["gate"]), "drain_in_mid": (xdi - 0.01, PROBE_DEPTHS["mid"]),
                "under_source_mid": (xsi - 0.01, PROBE_DEPTHS["mid"]), "under_drain_mid": (xdi + 0.01, PROBE_DEPTHS["mid"])})
    return pts


def deck_features(sdevice: str) -> dict[str, Any]:
    """Trap and tunneling features of a sdevice text (comments removed): bulk traps in the channel, the interface locations
    that carry traps, and nonlocal barrier tunneling."""
    feats: dict[str, Any] = {"bulk_traps": False, "interface_traps": [], "barrier_tunneling": False}
    for b in top_level_blocks(sdevice):
        if b["name"] != "Physics":
            continue
        if re.search(r"\bTraps\b", b["body"]):
            m = re.search(r'(RegionInterface|MaterialInterface)\s*=\s*"([^"]+)"', b["qual"])
            if m:
                feats["interface_traps"].append((m.group(1), m.group(2)))
            else:
                feats["bulk_traps"] = True
        if re.search(r"\beBarrierTunneling\b", b["body"]):
            feats["barrier_tunneling"] = True
    return feats


_FEATURE_TOKEN = re.compile(r'"[^"]*"|@[A-Za-z0-9_]+@|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?|[A-Za-z_][A-Za-z0-9_]*|[()=\[\]{}]')
# model groups whose first-level members are model choices (SRH vs Auger, HighFieldSaturation, ...)
_FEATURE_GROUPS = ("Mobility", "Recombination", "EffectiveIntrinsicDensity")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _body_keywords(body: str) -> set[str]:
    """Keywords at the top level of a Physics or Electrode body, and one level inside the model groups. Values after
    '=', quoted names, numbers and tokens are skipped."""
    out: set[str] = set()
    stack: list[str] = []
    prev = ""
    for m in _FEATURE_TOKEN.finditer(body):
        tok = m.group(0)
        if tok in "([{":
            stack.append(prev if _IDENT_RE.fullmatch(prev) else "")
        elif tok in ")]}":
            if stack:
                stack.pop()
        elif _IDENT_RE.fullmatch(tok) and prev != "=":
            if not stack:
                out.add(tok)
            elif len(stack) == 1 and stack[0] in _FEATURE_GROUPS:
                out.add(f"{stack[0]}.{tok}")
        prev = tok
    return out


def model_features(sdevice: str) -> set[str]:
    feats: set[str] = set()
    for b in top_level_blocks(sdevice):
        if b["name"] == "Physics":
            loc = re.sub(r'[\s"()]', "", b["qual"]) or "global"
            feats |= {f"{loc}:{k}" for k in _body_keywords(b["body"])}
        elif b["name"] == "Electrode":
            feats |= {f"Electrode:{k}" for k in _body_keywords(b["body"].replace("{", " ").replace("}", " "))
                      if k not in ("Name", "Voltage")}
    return feats


def unverified_model_features(templates: Mapping[str, str], base_templates: Mapping[str, str]) -> list[str]:
    """Model features that a patch adds to the start deck and that need an activation check."""
    added = model_features(templates["sdevice"]) - model_features(base_templates["sdevice"])
    return sorted(added)


def harness_currentplot(device: str, feats: Mapping[str, Any]) -> str:
    g = C.GEOMETRIES[C.DEVICES[device]["geometry"]]
    xsi, xdi = g["Lov_each_um"], g["Lov_each_um"] + g["L_um"]
    xend = 2 * g["Lov_each_um"] + g["L_um"]
    pts = probe_points(device)
    f = lambda v: f"{v:.6g}"  # noqa: E731
    plist = " ".join(f"({f(x)} {f(y)})" for x, y in pts.values())
    qpts = " ".join(f"({f(x)} {f(y)})" for k, (x, y) in pts.items()
                    if k in ("under_source_mid", "source_in_gate", "centre_gate", "drain_in_gate", "under_drain_mid"))
    win = lambda name, x0, x1: f'Integrate(Name="{name}" Window[({f(x0)} 0) ({f(x1)} {f(IWO_T_UM)})] {C.REGIONS["channel"]})'  # noqa: E731
    lines = ["CurrentPlot {", "   * harness-owned: internal quantities per bias point, no extra solves",
             f"   ConductionBandEnergy( {plist} )", f"   eQuasiFermiEnergy( {plist} )",
             f"   eQuasiFermiPotential( {qpts} )",
             f"   eDensity( {win('n_channel', xsi, xdi)} {win('n_under_source', 0, xsi)} {win('n_under_drain', xdi, xend)} )",
             f'   SpaceCharge( Integrate(Name="rho_iwo" {C.REGIONS["channel"]}) )']
    if feats.get("bulk_traps"):
        lines.append(f"   eTrappedCharge( {win('nt_channel', xsi, xdi)} Integrate(Name=\"nt_iwo\" {C.REGIONS['channel']}) )")
    for kind, loc in feats.get("interface_traps", []):
        tag = re.sub(r"[^A-Za-z0-9]+", "_", loc)
        lines.append(f'   eInterfaceTrappedCharge( Integrate(Name="nit_{tag}" {kind}="{loc}") )')
    if feats.get("barrier_tunneling"):
        lines.append(f'   eBarrierTunneling( Integrate(Name="g_tun_iwo" {C.REGIONS["channel"]}) )')
    lines.append("}")
    return "\n".join(lines) + "\n"


HARNESS_PLOT_FIELDS = {"bulk_traps": ("eTrappedCharge", "hTrappedCharge"),
                       "interface_traps": ("eInterfaceTrappedCharge", "hInterfaceTrappedCharge"),
                       "barrier_tunneling": ("eBarrierTunneling",)}


def add_harness_outputs(sdevice: str, device: str) -> str:
    """Append the harness CurrentPlot section and add the trap / tunneling fields to the Plot section."""
    feats = deck_features(sdevice)
    out = sdevice
    m = re.search(r"(?m)^Plot\s*\{", out)
    if m:
        depth, k = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(out[k], 0)
            k += 1
        body = out[m.end():k - 1]
        extra = [fld for key, flds in HARNESS_PLOT_FIELDS.items() if feats.get(key) for fld in flds
                 if not re.search(rf"\b{fld}\b", body)]
        if extra:
            out = out[:k - 1] + "   " + " ".join(extra) + "   * harness-owned\n" + out[k - 1:]
    return out.rstrip("\n") + "\n\n" + harness_currentplot(device, feats)


@dataclass
class Rendered:
    sde: str
    sdevice: str
    par: str
    values: dict[str, float]      # parameter values as written to the deck (6 significant digits)


def render(templates: Mapping[str, str], params: Mapping[str, Mapping[str, Any]], values: Mapping[str, float],
           device: str, vd: float, level: int = 0, threads: int | None = None) -> Rendered:
    """Fill every token. ``params`` is the registry subset active in this deck version."""
    geometry = C.DEVICES[device]["geometry"]
    tok: dict[str, str] = {**geometry_tokens(geometry), **solver_tokens(level, threads or C.THREADS), "VD": f"{vd:g}"}
    written: dict[str, float] = {}
    for name, spec in params.items():
        if name not in values:
            raise DeckGuardError(f"no value for parameter {name}")
        txt = fmt_value(float(values[name]), spec["scale"])
        tok[spec["token"].strip("@")] = txt
        written[name] = float(txt)
    out = {}
    for key, text in templates.items():
        def sub(m: re.Match) -> str:
            if m.group(1) not in tok:
                raise DeckGuardError(f"token @{m.group(1)}@ has no value")
            return tok[m.group(1)]
        lines = []
        for line in text.splitlines(keepends=True):
            s = line.strip()
            lines.append(line if s.startswith("*") or s.startswith(";") else TOKEN_RE.sub(sub, line))
        out[key] = "".join(lines)
    if C.TRAPDLN_PEAK_SAMPLING and "Traps" in strip_comments(out["sdevice"]):   # trap-free decks keep their cache keys
        if out["sdevice"].count(C.TRAPDLN_START_LINE + "\n") != 1:
            raise DeckGuardError("TrapDLN: the start-deck Math entry is missing")
        out["sdevice"] = out["sdevice"].replace(C.TRAPDLN_START_LINE + "\n",
                                                f"   TrapDLN(PeakSampling) = {C.TRAPDLN_PEAK_SAMPLING}\n")
    if C.HARNESS_OUTPUTS:
        out["sdevice"] = add_harness_outputs(out["sdevice"], device)
    if C.RECOVERY_LEVELS[level].get("MESH_SCALE"):
        out["sde"] = refine_mesh(out["sde"], C.RECOVERY_LEVELS[level]["MESH_SCALE"])
    r = Rendered(sde=out["sde"], sdevice=out["sdevice"], par=out["par"], values=written)
    guard_rendered(r, params, device, vd)
    return r


def guard_rendered(r: Rendered, params: Mapping[str, Mapping[str, Any]], device: str, vd: float) -> None:
    errs: list[str] = []
    for key in ("sde", "sdevice", "par"):
        if tokens_in(strip_comments(getattr(r, key))):
            errs.append(f"{key}: unfilled tokens {sorted(tokens_in(strip_comments(getattr(r, key))))}")
    g = C.GEOMETRIES[C.DEVICES[device]["geometry"]]

    def sde_val(name: str) -> float:
        m = re.search(rf"\(define {name}\s+([-+0-9.eE]+)\)", r.sde)
        if not m:
            raise DeckGuardError(f"SDE define {name} missing")
        return float(m.group(1))
    xs_in, xd_in, xs_out, xd_out = (sde_val(k) for k in ("x_source_inner", "x_drain_inner", "x_source_outer", "x_drain_outer"))
    if abs((xd_in - xs_in) - g["L_um"]) > 1e-9 or abs(sde_val("L_um") - g["L_um"]) > 1e-9:
        errs.append(f"SDE channel gap {xd_in - xs_in} != L {g['L_um']} for device {device}")
    if abs((xs_in - xs_out) - g["Lov_each_um"]) > 1e-9 or abs((xd_out - xd_in) - g["Lov_each_um"]) > 1e-9:
        errs.append(f"SDE overlap != {g['Lov_each_um']:g} um per side")
    dv = C.DECK_VARIABLES
    nm = lambda key: sde_val(dv[key]["name"]) * dv[key]["to_nm"]  # noqa: E731
    if abs(nm("t_channel") - C.DEVICE_SPEC[C.T_CH_KEY]) > 1e-9 or abs(nm("t_gate_dielectric") - C.DEVICE_SPEC["t_gox_nm"]) > 1e-9:
        errs.append("SDE thickness mismatch")
    if abs(nm("t_passivation") - C.DEVICE_SPEC["t_pass_nm"]) > 1e-9:
        errs.append("SDE passivation thickness mismatch")
    if dv.get("contact_height_define") and not re.search(dv["contact_height_define"], r.sde):
        errs.append(f"SDE S/D height is not {C.DEVICE_SPEC['h_sd_nm']:g} nm")
    m = re.search(r"AreaFactor\s*=\s*([-+0-9.eE]+)", r.sdevice)
    if not m or float(m.group(1)) != C.DEVICE_SPEC["AreaFactor"]:
        errs.append(f"AreaFactor != {C.DEVICE_SPEC['AreaFactor']:g}")
    m = re.search(r'Goal \{ Name="drain" Voltage=([-+0-9.eE]+) \}', r.sdevice)
    if not m or abs(float(m.group(1)) - vd) > 1e-12:
        errs.append("drain goal != requested VD")
    goals = [float(x) for x in re.findall(r'Goal \{ Name="gate" Voltage=([-+0-9.eE]+) \}', r.sdevice)]
    if len(goals) != 2 or goals[0] > C.VG_SIM_START + 1e-9 or goals[1] < C.VG_SIM_STOP - 1e-9:
        errs.append(f"gate sweep {goals} does not cover {C.VG_SIM_START} -> {C.VG_SIM_STOP} V")
    hf = par_material_blocks(r.par).get(C.GATE_DIELECTRIC, "")
    eps = re.findall(r"(Epsilon(?:_aniso)?)\s*\{\s*epsilon\s*=\s*([-+0-9.eE]+)\s*\}", hf)
    if sorted(k for k, _ in eps) != ["Epsilon", "Epsilon_aniso"] or any(float(v) != C.DEVICE_SPEC[C.K_GD_KEY] for _, v in eps):
        errs.append(f"{C.GATE_DIELECTRIC} epsilon must be {C.DEVICE_SPEC[C.K_GD_KEY]:g} (Epsilon and Epsilon_aniso), found {eps}")
    nd = sde_val("Nd_eff")
    if "Nd_eff" in r.values and abs(nd / r.values["Nd_eff"] - 1) > 1e-9:
        errs.append("Nd_eff in SDE != requested")
    if errs:
        raise DeckGuardError("; ".join(errs))


def iwo_par_epsilon(path: Path) -> list[float]:
    text = Path(path).read_text(errors="ignore")
    return [float(v) for v in re.findall(r"Epsilon(?:_aniso)?\s*\{[^}]*?\bepsilon\s*=\s*([-+0-9.eE]+)", text)]


def copy_support_files(dest: Path) -> dict[str, str]:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = {}
    for name in C.SYNOPSYS_SUPPORT_FILES:
        shutil.copy2(C.SYNOPSYS_SUPPORT_DIR / name, dest / name)
        out[name] = md5_file(dest / name)
    eps = iwo_par_epsilon(dest / C.CHANNEL_PAR)
    if eps != [C.DEVICE_SPEC[C.K_CH_KEY]] * 2:
        raise DeckGuardError(f"{C.CHANNEL_PAR} epsilon must be {C.DEVICE_SPEC[C.K_CH_KEY]:g} (Epsilon and Epsilon_aniso), found {eps}")
    return out


# ---------------------------------------------------------------------------
# Patches (applied only from an approved proposal)
# ---------------------------------------------------------------------------
def apply_edits(templates: Mapping[str, str], edits: list[Mapping[str, str]]) -> dict[str, str]:
    out = dict(templates)
    for k, e in enumerate(edits):
        key = e.get("file")
        if key not in ("sdevice", "par"):
            raise DeckGuardError(f"edit {k}: file must be 'sdevice' or 'par' (structure is fixed), got {key!r}")
        old, new = e.get("old", ""), e.get("new", "")
        if not old:
            raise DeckGuardError(f"edit {k}: empty 'old' string")
        n = out[key].count(old)
        if e.get("all") is True:
            # replace every occurrence (e.g. the same line in several solve statements)
            if n < 1:
                raise DeckGuardError(f"edit {k}: 'old' text not found in {key}")
        elif n != 1:
            raise DeckGuardError(f"edit {k}: 'old' text found {n} times in {key} (must be exactly once, or set \"all\": true)")
        out[key] = out[key].replace(old, new)
    return out
