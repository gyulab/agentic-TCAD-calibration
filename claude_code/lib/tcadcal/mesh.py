"""Geometry that Sentaurus actually uses, read back from the mesh TDR (tdx -dd -> DF-ISE).

Includes the passivation thickness and the S/D contact height. ``assert_device_spec``
is called on every new mesh before any sdevice run.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import config as C


def _section(lines: list[str], start: int) -> tuple[list[str], int]:
    depth, body, i = 0, [], start
    while i < len(lines):
        depth += lines[i].count("{") - lines[i].count("}")
        if i > start:
            if depth <= 0:
                return body, i
            body.append(lines[i])
        i += 1
    raise ValueError("unterminated block")


def parse_grd(grd: Path) -> dict[str, Any]:
    lines = Path(grd).read_text().splitlines()
    idx = {k: next(i for i, s in enumerate(lines) if s.strip().startswith(k)) for k in ("Vertices", "Edges", "Elements")}
    verts = [tuple(map(float, s.split())) for s in _section(lines, idx["Vertices"])[0] if s.strip()]
    edges = [tuple(map(int, s.split())) for s in _section(lines, idx["Edges"])[0] if s.strip()]
    toks = " ".join(_section(lines, idx["Elements"])[0]).split()
    elems, k, nargs = [], 0, {0: 1, 1: 2, 2: 3, 3: 4}
    while k < len(toks):
        t = int(toks[k])
        n = nargs[t]
        elems.append((t, [int(x) for x in toks[k + 1:k + 1 + n]]))
        k += 1 + n

    def elem_vertices(e: tuple[int, list[int]]) -> set[int]:
        t, ids = e
        if t in (0, 1):
            return set(ids)
        out: set[int] = set()
        for ei in ids:
            out.update(edges[ei if ei >= 0 else -ei - 1])
        return out

    regions = {}
    for i, s in enumerate(lines):
        st = s.strip()
        if st.startswith('Region ("'):
            name = st.split('"')[1]
            body, _ = _section(lines, i)
            mat = next(b.split("=")[1].strip() for b in body if b.strip().startswith("material"))
            j = next(n for n, b in enumerate(body) if b.strip().startswith("Elements"))
            ids = [int(x) for x in " ".join(body[j + 1:]).replace("}", " ").split()]
            vs: set[int] = set()
            tris: list[tuple[int, ...]] = []
            for e in ids:
                ev = elem_vertices(elems[e])
                vs |= ev
                if elems[e][0] == 2:
                    tris.append(tuple(sorted(ev)))
            xs = [verts[v][0] for v in vs]
            ys = [verts[v][1] for v in vs]
            regions[name] = {"material": mat, "n_elements": len(ids), "xmin": min(xs), "xmax": max(xs),
                             "ymin": min(ys), "ymax": max(ys), "_v": vs, "_tri": tris}
    return {"vertices": verts, "regions": regions}


def parse_doping(dat: Path) -> dict[str, list[float]]:
    """Per-region [min, max] of the donor species that SDE wrote into the mesh (DF-ISE .dat from tdx -dd)."""
    out: dict[str, list[float]] = {}
    for m in re.finditer(r'Dataset \("[^"]*"\) \{(.*?)Values \(\d+\) \{(.*?)\}', Path(dat).read_text(), re.S):
        fn = re.search(r"function\s*=\s*(\S+)", m.group(1))
        reg = re.search(r'validity\s*=\s*\[\s*"([^"]+)"', m.group(1))
        if fn and reg and fn.group(1) == "PhosphorusActiveConcentration":
            v = [float(x) for x in m.group(2).split()]
            out[reg.group(1)] = [min(v), max(v)]
    return out


def derive(g: dict[str, Any]) -> dict[str, Any]:
    r, verts = g["regions"], g["vertices"]
    semi = next(v for v in r.values() if v["material"] == C.CHANNEL)
    gate, src, drn = r["gate"], r["source"], r["drain"]

    def on_semi(c: dict) -> list[float]:
        return sorted(verts[v][0] for v in c["_v"] & semi["_v"])
    sx, dx = on_semi(src), on_semi(drn)
    out: dict[str, Any] = {
        "materials": sorted({v["material"] for v in r.values() if v["material"] not in ("Interface", "Contact")}),
        C.T_CH_KEY: 1e3 * (semi["ymax"] - semi["ymin"]),
        "t_gox_nm": 1e3 * (r[C.REGIONS["gate_dielectric"]]["ymax"] - r[C.REGIONS["gate_dielectric"]]["ymin"]),
        "gox_material": r[C.REGIONS["gate_dielectric"]]["material"],
        "t_pass_nm": 1e3 * (r[C.REGIONS["passivation_top"]]["ymax"] - r[C.REGIONS["passivation_top"]]["ymin"]),
        "pass_material": r[C.REGIONS["passivation_top"]]["material"],
        "h_sd_source_nm": 1e3 * (src["ymax"] - src["ymin"]),
        "h_sd_drain_nm": 1e3 * (drn["ymax"] - drn["ymin"]),
        "L_gap_um": dx[0] - sx[-1],
        "Lov_source_um": max(0.0, min(sx[-1], gate["xmax"]) - max(sx[0], gate["xmin"])),
        "Lov_drain_um": max(0.0, min(dx[-1], gate["xmax"]) - max(dx[0], gate["xmin"])),
        "n_vertices": len(verts),
    }
    out["regions"] = {k: {kk: vv for kk, vv in v.items() if kk not in ("_v", "_tri")} for k, v in r.items()}
    out["resolution"] = resolution(g, sx[-1], dx[0])
    return out


def _cuts(verts: list[tuple[float, ...]], tris: list[tuple[int, ...]], axis: int, at: float) -> list[float]:
    """Sorted unique coordinates (along the other axis) where the line coord[axis] = at crosses the triangle edges."""
    other = 1 - axis
    pts: list[float] = []
    for t in tris:
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[0], t[2])):
            pa, pb = verts[a], verts[b]
            da, db = pa[axis] - at, pb[axis] - at
            if abs(da) < 1e-12 and abs(db) < 1e-12:
                pts += [pa[other], pb[other]]
            elif da * db <= 0.0 and pa[axis] != pb[axis]:
                pts.append(pa[other] + (at - pa[axis]) * (pb[other] - pa[other]) / (pb[axis] - pa[axis]))
    pts.sort()
    out: list[float] = []
    for v in pts:
        if not out or v - out[-1] > 1e-7:
            out.append(v)
    return out


def resolution(g: dict[str, Any], x_si: float, x_di: float) -> dict[str, Any]:
    """Mesh resolution in the channel film (lengths in nm). x_si / x_di: inner source / drain contact edges (um)."""
    verts = g["vertices"]
    semi = [v for v in g["regions"].values() if v["material"] == C.CHANNEL]
    tris = [t for v in semi for t in v["_tri"]]
    y0, y1 = min(v["ymin"] for v in semi), max(v["ymax"] for v in semi)
    xs = {"centre": 0.5 * (x_si + x_di), "source_edge_in": x_si + 0.010, "drain_edge_in": x_di - 0.010,
          "under_source": x_si - 0.010, "under_drain": x_di + 0.010}
    vertical = {}
    for name, x in xs.items():
        ys = [y for y in _cuts(verts, tris, 0, x) if y0 - 1e-9 <= y <= y1 + 1e-9]
        steps = [1e3 * (b - a) for a, b in zip(ys, ys[1:])]
        vertical[name] = {"x_um": round(x, 6), "rows": len(ys), "first_bottom_nm": round(steps[0], 4) if steps else None,
                          "first_top_nm": round(steps[-1], 4) if steps else None,
                          "max_step_nm": round(max(steps), 4) if steps else None}
    lateral = {}
    for name, xe in (("source_edge", x_si), ("drain_edge", x_di)):
        for depth, yy in (("gate_side", y0 + 0.25 * (y1 - y0)), ("mid", 0.5 * (y0 + y1))):
            xs_line = [x for x in _cuts(verts, tris, 1, yy) if xe - 0.05 <= x <= xe + 0.05]
            st = [1e3 * (b - a) for a, b in zip(xs_line, xs_line[1:])]
            near = [1e3 * (b - a) for a, b in zip(xs_line, xs_line[1:]) if min(abs(a - xe), abs(b - xe)) <= 0.0021]
            lateral[f"{name}_{depth}"] = {"points_within_50nm": len(xs_line),
                                          "max_step_nm": round(max(st), 4) if st else None,
                                          "step_at_edge_nm": round(max(near), 4) if near else None}
    centre_line = [x for x in _cuts(verts, tris, 1, 0.5 * (y0 + y1))
                   if x_si + 0.1 <= x <= x_di - 0.1]
    cst = [1e3 * (b - a) for a, b in zip(centre_line, centre_line[1:])]
    return {"vertical": vertical, "lateral": lateral,
            "channel_interior_max_lateral_nm": round(max(cst), 4) if cst else None,
            "n_vertices_iwo": len(set().union(*(v["_v"] for v in semi)))}


def assert_resolution(readback: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """Violations of a mesh-resolution spec (config.MESH_RESOLUTION_MIN). Empty = pass."""
    res = readback.get("resolution")
    if not res:
        return ["mesh resolution read-back missing"]
    errs = []
    for name, v in res["vertical"].items():
        if v["rows"] < spec["min_rows_iwo"]:
            errs.append(f"{C.CHANNEL} film at {name} (x = {v['x_um']} um) has {v['rows']} vertex rows (spec >= {spec['min_rows_iwo']})")
        for k in ("first_bottom_nm", "first_top_nm"):
            if v[k] is None or v[k] > spec["max_first_element_nm"] + 1e-6:
                errs.append(f"{C.CHANNEL} film at {name}: {k} {v[k]} nm (spec <= {spec['max_first_element_nm']} nm)")
    for name, v in res["lateral"].items():
        if v["step_at_edge_nm"] is None or v["step_at_edge_nm"] > spec["max_edge_lateral_nm"] + 1e-6:
            errs.append(f"lateral step at {name} {v['step_at_edge_nm']} nm (spec <= {spec['max_edge_lateral_nm']} nm)")
    if res["channel_interior_max_lateral_nm"] is None or res["channel_interior_max_lateral_nm"] > spec["max_interior_lateral_nm"] + 1e-6:
        errs.append(f"channel interior lateral step {res['channel_interior_max_lateral_nm']} nm (spec <= "
                    f"{spec['max_interior_lateral_nm']} nm)")
    return errs


def grid_hash(g: dict[str, Any]) -> str:
    h = hashlib.sha256()
    for v in g["vertices"]:
        h.update((" ".join(repr(float(x)) for x in v) + "\n").encode())
    for name in sorted(g["regions"]):
        r = g["regions"][name]
        h.update(f"{name}|{r['material']}\n".encode())
        for t in sorted(r["_tri"]):
            h.update((" ".join(map(str, t)) + "\n").encode())
    return h.hexdigest()[:16]


def read_mesh(tdr: Path) -> dict[str, Any]:
    tdr = Path(tdr).resolve()
    with tempfile.TemporaryDirectory(dir=os.environ.get("TCADCAL_TMP", str(C.ROOT / "tmp"))) as td:
        work = Path(td)
        shutil.copy2(tdr, work / "m.tdr")
        env = dict(os.environ, **C.LICENSE_ENV)
        p = subprocess.run([str(C.SENTAURUS_BIN / "tdx"), "-dd", "m.tdr", "m"], cwd=work, env=env,
                           capture_output=True, text=True, timeout=300)
        grd = work / "m.grd"
        if not grd.exists():
            raise RuntimeError(f"tdx failed for {tdr}: {p.stdout[-400:]} {p.stderr[-400:]}")
        g = parse_grd(grd)
        out = derive(g)
        out["grid_hash"] = grid_hash(g)
        dop = parse_doping(work / "m.dat") if (work / "m.dat").exists() else {}
        vals = [x for n, v in out["regions"].items() if v["material"] == C.CHANNEL for x in dop.get(n, [0.0, 0.0])]
        out["channel_doping_cm3"] = [min(vals), max(vals)]
    out["tdr"] = str(tdr)
    return out


def assert_doping(readback: dict[str, Any], nd_requested: float, rel_tol: float = 1e-4) -> list[str]:
    """Return a violation if the channel donor density in the mesh differs from the requested N_D,eff."""
    lo, hi = readback.get("channel_doping_cm3", [0.0, 0.0])
    if abs(lo / nd_requested - 1) > rel_tol or abs(hi / nd_requested - 1) > rel_tol:
        return [f"channel donor density in the mesh {lo:.4g}-{hi:.4g} cm^-3 (requested N_D,eff {nd_requested:.4g} cm^-3)"]
    return []


def assert_device_spec(readback: dict[str, Any], device: str) -> list[str]:
    g = C.GEOMETRIES[C.DEVICES[device]["geometry"]]
    t, tol = C.DEVICE_SPEC, C.GEOM_TOL_UM
    checks = [
        ("L_gap_um", g["L_um"], tol), ("Lov_source_um", t["Lov_each_um"], tol), ("Lov_drain_um", t["Lov_each_um"], tol),
        (C.T_CH_KEY, t[C.T_CH_KEY], 1e-3), ("t_gox_nm", t["t_gox_nm"], 1e-3), ("t_pass_nm", t["t_pass_nm"], 1e-3),
        ("h_sd_source_nm", t["h_sd_nm"], 1e-3), ("h_sd_drain_nm", t["h_sd_nm"], 1e-3),
    ]
    errs = [f"{k} = {readback[k]:.6g} (specification {want:g})" for k, want, tl in checks if abs(readback[k] - want) > tl]
    if readback["gox_material"] != C.GATE_DIELECTRIC or readback["pass_material"] != C.PASSIVATION:
        want = C.GATE_DIELECTRIC if C.GATE_DIELECTRIC == C.PASSIVATION else f"{C.GATE_DIELECTRIC}/{C.PASSIVATION}"
        errs.append(f"dielectric materials {readback['gox_material']}/{readback['pass_material']} (specification {want})")
    if set(readback["materials"]) != {C.CHANNEL, C.GATE_DIELECTRIC, C.PASSIVATION}:
        errs.append(f"unexpected materials {readback['materials']}")
    return errs


SNAPSHOT_FIELDS = ("ConductionBandEnergy", "ValenceBandEnergy", "eQuasiFermiEnergy", "ElectrostaticPotential",
                   "eDensity", "eTrappedCharge", "eInterfaceTrappedCharge", "eBarrierTunneling", "SpaceCharge",
                   "EffectiveBandGap", "ElectronAffinity")
VECTOR_FIELDS = ("eCurrentDensity",)


def parse_dat(dat: Path, functions: tuple[str, ...] = SNAPSHOT_FIELDS + VECTOR_FIELDS) -> dict[str, dict[str, Any]]:
    """{function: {region: np.ndarray}} for the listed functions (vector fields: shape (n, 2))."""
    import numpy as np
    out: dict[str, dict[str, Any]] = {}
    text = Path(dat).read_text()
    for m in re.finditer(r'Dataset \("[^"]*"\) \{(.*?)Values \((\d+)\) \{(.*?)\}', text, re.S):
        fn = re.search(r"function\s*=\s*(\S+)", m.group(1))
        reg = re.search(r'validity\s*=\s*\[\s*"([^"]+)"', m.group(1))
        if not fn or not reg or fn.group(1) not in functions:
            continue
        v = np.array(m.group(3).split(), float)
        dim = re.search(r"dimension\s*=\s*(\d+)", m.group(1))
        if dim and int(dim.group(1)) > 1:
            v = v.reshape(-1, int(dim.group(1)))
        out.setdefault(fn.group(1), {})[reg.group(1)] = v
    return out


def _cut_region(verts, tris, vids, vals: dict[str, Any], axis: int, at: float) -> list[tuple[float, dict[str, float]]]:
    import numpy as np
    pos = {v: i for i, v in enumerate(vids)}
    other = 1 - axis
    pts = []
    for t in tris:
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[0], t[2])):
            pa, pb = verts[a], verts[b]
            da, db = pa[axis] - at, pb[axis] - at
            if da * db > 0.0 or (abs(da) < 1e-12 and abs(db) < 1e-12) or pa[axis] == pb[axis]:
                if abs(da) < 1e-12 and abs(db) >= 1e-12:
                    w = 0.0
                elif abs(db) < 1e-12 and abs(da) >= 1e-12:
                    w = 1.0
                else:
                    continue
            else:
                w = (at - pa[axis]) / (pb[axis] - pa[axis])
            c = pa[other] + w * (pb[other] - pa[other])
            rec = {}
            for f, arr in vals.items():
                va, vb = arr[pos[a]], arr[pos[b]]
                rec[f] = (1 - w) * np.asarray(va) + w * np.asarray(vb)
            pts.append((c, rec))
    pts.sort(key=lambda p: p[0])
    out = []
    for c, rec in pts:
        if not out or c - out[-1][0] > 1e-7:
            out.append((c, rec))
    return out


def extract_snapshot(tdr: Path, device: str, regions: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Cuts through one plot TDR: vertical cuts at the probe x positions (all listed regions, default: the gate
    dielectric, channel and passivation regions of device.toml), lateral cuts along the channel film at the gate side
    and mid film, plus the channel material read-back (effective band gap, affinity)."""
    if regions is None:
        regions = (C.REGIONS["gate_dielectric"], C.REGIONS["channel"], C.REGIONS["passivation_top"])
    import numpy as np
    tdr = Path(tdr).resolve()
    with tempfile.TemporaryDirectory(dir=os.environ.get("TCADCAL_TMP", str(C.ROOT / "tmp"))) as td:
        work = Path(td)
        shutil.copy2(tdr, work / "s.tdr")
        env = dict(os.environ, **C.LICENSE_ENV)
        subprocess.run([str(C.SENTAURUS_BIN / "tdx"), "-dd", "s.tdr", "s"], cwd=work, env=env, capture_output=True,
                       text=True, timeout=300)
        if not (work / "s.grd").exists() or not (work / "s.dat").exists():
            raise RuntimeError(f"tdx failed for {tdr}")
        g = parse_grd(work / "s.grd")
        data = parse_dat(work / "s.dat")
    verts, R = g["vertices"], g["regions"]
    gcfg = C.GEOMETRIES[C.DEVICES[device]["geometry"]]
    xsi, xdi = gcfg["Lov_each_um"], gcfg["Lov_each_um"] + gcfg["L_um"]

    def region_vals(name: str) -> tuple[list[int], dict[str, Any]]:
        vids = sorted(R[name]["_v"])
        vals = {}
        for f, per in data.items():
            if name in per and len(per[name]) == len(vids):
                arr = per[name]
                vals[f] = arr[:, 0] if arr.ndim == 2 else arr   # vectors: x component; else magnitude (A/cm^2)
        return vids, vals

    out: dict[str, Any] = {"tdr": tdr.name}
    gox = R[C.REGIONS["gate_dielectric"]]
    gv, gvals = region_vals(C.REGIONS["gate_dielectric"])
    ybot = min(verts[v][1] for v in gv)
    phi = [gvals["ElectrostaticPotential"][i] for i, v in enumerate(gv) if abs(verts[v][1] - ybot) < 1e-9] \
        if "ElectrostaticPotential" in gvals else []
    out["phi_gate_boundary_V"] = float(np.mean(phi)) if phi else None
    sv, svals = region_vals(C.REGIONS["channel"])
    mat = {}
    for f in ("EffectiveBandGap", "ElectronAffinity"):
        if f in svals:
            mat[f] = [float(np.min(svals[f])), float(np.max(svals[f]))]
    out[C.CH_MAT_KEY] = mat
    vcuts = {}
    for name, x in {"centre": 0.5 * (xsi + xdi), "source_in": xsi + 0.01, "drain_in": xdi - 0.01,
                    "under_source": xsi - 0.01, "under_drain": xdi + 0.01}.items():
        rows = []
        for reg in regions:
            if reg not in R:
                continue
            vids, vals = region_vals(reg)
            for c, rec in _cut_region(verts, R[reg]["_tri"], vids, vals, 0, x):
                rows.append({"region": reg, "y_nm": round(1e3 * c, 5), **{k: float(v) for k, v in rec.items()}})
        vcuts[name] = sorted(rows, key=lambda r: (r["y_nm"], r["region"]))
    out["vertical"] = vcuts
    lcuts = {}
    lat_fields = ("ConductionBandEnergy", "eQuasiFermiEnergy", "eDensity", "eCurrentDensity", "ElectrostaticPotential")
    for name, y in (("gate_side", 0.0001), ("mid", 0.00175)):
        vals = {k: v for k, v in svals.items() if k in lat_fields}
        lcuts[name] = [{"x_um": round(c, 6), **{k: float(v) for k, v in rec.items()}}
                       for c, rec in _cut_region(verts, R[C.REGIONS["channel"]]["_tri"], sv, vals, 1, y)]
    out["lateral"] = lcuts
    return out
