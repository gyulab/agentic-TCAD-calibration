"""Sentaurus execution: mesh (SDE) with read-back of the device specification, sdevice runs with the
numerical-recovery sequence, hash-cached run directories, run budget, and run manifests."""
from __future__ import annotations

import concurrent.futures as cf
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from . import config as C
from . import deck as D
from . import mesh as MESH
from .util import append_jsonl, cpu_slot, file_lock, now_utc, read_json, sha256_text, write_json


class BudgetExhausted(RuntimeError):
    pass


class GeometryViolation(RuntimeError):
    pass


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(C.LICENSE_ENV)
    env["PATH"] = f"{C.SENTAURUS_BIN}:{env.get('PATH', '')}"
    return env


def _run(argv: list[str], cwd: Path, log_name: str, timeout_s: int) -> dict[str, Any]:
    t0 = time.time()
    with (cwd / log_name).open("w") as fh:
        fh.write(f"$ {' '.join(argv)}\nstarted_utc={now_utc()}\n\n")
        fh.flush()
        try:
            proc = subprocess.run(argv, cwd=str(cwd), env=_env(), stdout=fh, stderr=subprocess.STDOUT,
                                  timeout=timeout_s, check=False)
            rc, status = proc.returncode, ("success" if proc.returncode == 0 else "failed")
        except subprocess.TimeoutExpired:
            rc, status = None, "timeout"
        fh.write(f"\nfinished_utc={now_utc()}\nreturncode={rc}\n")
    return {"status": status, "returncode": rc, "seconds": round(time.time() - t0, 2)}


# ---------------------------------------------------------------------------
# Budget (sdevice launches; cache hits are free)
# ---------------------------------------------------------------------------
class Budget:
    def __init__(self, path: Path, cap: int | None):
        self.path, self.cap = Path(path), cap

    def take(self, what: str, category: str = "calibration") -> int:
        with file_lock(self.path.with_suffix(".lock")):
            b = read_json(self.path, {"used": 0, "cap": self.cap})
            cap = self.cap if self.cap is not None else b.get("cap")
            if cap is not None and b["used"] >= cap:
                raise BudgetExhausted(f"sdevice run budget exhausted ({b['used']}/{cap})")
            cats = b.setdefault("by_category", {})
            ccap = C.BUDGET_CATEGORY_CAPS.get(category)
            if ccap is not None and cats.get(category, 0) >= ccap:
                raise BudgetExhausted(f"{category} runs exhausted ({cats.get(category, 0)}/{ccap}, inside the total budget)")
            cats[category] = cats.get(category, 0) + 1
            b["used"] += 1
            b["cap"] = cap
            b["last"] = {"what": what, "utc": now_utc()}
            write_json(self.path, b)
            return b["used"]

    def used(self) -> dict[str, Any]:
        return read_json(self.path, {"used": 0, "cap": self.cap})


# ---------------------------------------------------------------------------
# Mesh
# ---------------------------------------------------------------------------
def ensure_mesh(store: Path, sde_text: str, device: str, violations_log: Path | None = None) -> dict[str, Any]:
    store = Path(store).resolve()   # support-file symlinks must be absolute
    db_md5 = read_json(Path(store) / "support_md5.json", {}).get("datexcodes.txt")
    h = sha256_text("\n\x00".join([sde_text, str(db_md5)]))[:16]
    mdir = Path(store) / "mesh" / h
    done = mdir / "readback.json"
    with file_lock(mdir.parent / f"{h}.lock"):
        if done.exists():
            rb = read_json(done)
        else:
            mdir.mkdir(parents=True, exist_ok=True)
            for stale in mdir.glob("n1_msh*"):
                stale.unlink()
            (mdir / "sde_dvs.cmd").write_text(sde_text)
            for name in C.SYNOPSYS_SUPPORT_FILES:
                if not (mdir / name).exists():
                    os.symlink(Path(store) / "support" / name, mdir / name)
            res = _run([str(C.SENTAURUS_BIN / "sde"), "-e", "-l", "sde_dvs.cmd"], mdir, "sde_capture.log", C.SDE_TIMEOUT_S)
            if not (mdir / "n1_msh.tdr").exists():
                raise RuntimeError(f"SDE failed ({res['status']}), see {mdir / 'sde_capture.log'}")
            geo = MESH.read_mesh(mdir / "n1_msh.tdr")
            rb = {"mesh_hash": h, "device": device, "geometry": C.DEVICES[device]["geometry"], "sde": res,
                  "readback": {k: v for k, v in geo.items() if k not in ("regions", "tdr")}, "utc": now_utc()}
            rb["violations"] = MESH.assert_device_spec(geo, device)
            nd = re.search(r"\(define Nd_eff\s+([-+0-9.eE]+)\)", sde_text)
            if nd:
                rb["violations"] += MESH.assert_doping(geo, float(nd.group(1)))
            write_json(done, rb)
    if rb["device"] != device and C.DEVICES[rb["device"]]["geometry"] != C.DEVICES[device]["geometry"]:
        raise GeometryViolation(f"mesh {h} was built for {rb['device']}, requested for {device}")
    viol = rb["violations"]
    if viol:
        if violations_log:
            append_jsonl(violations_log, {"utc": now_utc(), "kind": "geometry", "mesh": h, "device": device, "violations": viol})
        raise GeometryViolation(f"mesh {h} fails the mesh check (device specification, channel doping) for {device}: {viol}")
    return {"mesh_hash": h, "dir": str(mdir), "tdr": str(mdir / "n1_msh.tdr"), "readback": rb["readback"]}


def mesh_grid_hash(mesh: Mapping[str, Any]) -> str:
    """Grid hash of a stored mesh (meshes built before the hash existed are read back once and cached)."""
    gh = (mesh.get("readback") or {}).get("grid_hash")
    if gh:
        return gh
    cache = Path(mesh["dir"]) / "grid_hash.txt"
    if cache.exists():
        return cache.read_text().strip()
    gh = MESH.read_mesh(Path(mesh["tdr"]))["grid_hash"]
    cache.write_text(gh + "\n")
    return gh


def _check_frozen_mesh(mesh: Mapping[str, Any], device: str, mesh_scale: float | None, level: int,
                          violations_log: Path | None) -> None:
    if not C.FROZEN_MESH_HASHES:
        return
    scale = (mesh_scale or 1.0) * C.RECOVERY_LEVELS[level].get("MESH_SCALE", 1.0)
    key = f"{C.DEVICES[device]['geometry']}|{scale:g}"
    want = C.FROZEN_MESH_HASHES.get(key)
    if want is None:
        return
    got = mesh_grid_hash(mesh)
    if got != want:
        if violations_log:
            append_jsonl(violations_log, {"utc": now_utc(), "kind": "frozen_mesh", "mesh": mesh["mesh_hash"],
                                          "device": device, "key": key, "grid_hash": got, "frozen": want})
        raise GeometryViolation(f"mesh {mesh['mesh_hash']} has grid {got}, not the frozen grid of this geometry {want} ({key})")


# ---------------------------------------------------------------------------
# sdevice
# ---------------------------------------------------------------------------
def parse_plt(path: Path) -> pd.DataFrame:
    text = Path(path).read_text(errors="ignore")
    m = re.search(r"datasets\s*=\s*\[(.*?)\]", text, re.S)
    names = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    dm = re.search(r"Data\s*\{(.*?)\n\s*\}", text, re.S)
    if not names or not dm:
        raise ValueError(f"cannot parse {path}")
    toks = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", dm.group(1))
    n = len(names)
    arr = np.array([float(t) for t in toks[: (len(toks) // n) * n]], float).reshape(-1, n)
    vg = arr[:, names.index("gate OuterVoltage")]
    vd = arr[:, names.index("drain OuterVoltage")]
    i_d = arr[:, names.index("drain TotalCurrent")]
    df = pd.DataFrame({"Vg": vg, "Id": i_d, "Vd": vd})
    return df.groupby("Vg", as_index=False).agg({"Id": "mean", "Vd": "median"}).sort_values("Vg").reset_index(drop=True)


LICENSE_FAIL_RE = re.compile(r"(?i)(unable to check ?out|license (server|error|request|not available)|cannot (connect|check ?out)"
                             r"|flexnet|flexlm|lmgrd|no such feature|licen[sc]e .*(denied|expired|unavailable))")


def classify_failure(log_text: str, status: str) -> tuple[str, str]:
    if status == "timeout":
        return "timeout", ""
    solve_at = re.search(r"^Solve :", log_text, re.M)
    errs = [m for m in re.finditer(r"^Error:\s*(.*)$", log_text, re.M)]
    if errs and (solve_at is None or errs[0].start() < solve_at.start()):
        return "deck error before any solve", errs[0].group(1).strip()[:200]
    lic = LICENSE_FAIL_RE.search(log_text)
    if lic:
        return "license", lic.group(0)[:120]
    return "solve failure", errs[-1].group(1).strip()[:200] if errs else ""


def _sweep_ok(df: pd.DataFrame) -> tuple[bool, str]:
    if df.empty:
        return False, "empty sweep"
    if df["Vg"].min() > C.VG_SIM_START + C.VG_TOL_V:
        return False, f"sweep starts at {df['Vg'].min():.4f} V"
    if df["Vg"].max() < C.VG_SIM_STOP - C.VG_TOL_V:
        return False, f"sweep stopped at VG={df['Vg'].max():.4f} V"
    if not np.all(np.isfinite(df["Id"].to_numpy(float))):
        return False, "non-finite current"
    return True, ""


def dense_snapshots(sdevice: str, spacing: float) -> str:
    """Replace the snapshot time list of the gate sweep by every n-th point of the 96-interval current-plot grid
    (n = spacing / grid step, at least 1), so the snapshots stay on solved steps."""
    import re as _re
    n = max(1, round(spacing / (1.0 / 96)))
    times = "; ".join(f"{k / 96:.10g}" for k in range(0, 97, n))
    new, cnt = _re.subn(r'Plot \( FilePrefix = "n1_snap" Time=\( [^)]* \) NoOverwrite \)',
                        f'Plot ( FilePrefix = "n1_snap" Time=( {times} ) NoOverwrite )', sdevice)
    if cnt != 1:
        raise D.DeckGuardError("dense_snapshots: the gate-sweep snapshot list was not found once")
    return new


def recovery_levels(levels: list[int] | None = None, max_level: int | None = None) -> list[int]:
    if levels is not None:
        return list(levels)
    return list(range(max_level + 1)) if max_level is not None else list(C.CALIBRATION_RECOVERY_LEVELS)


def run_one(store: Path, templates: Mapping[str, str], spec: Mapping[str, Any], values: Mapping[str, float],
            device: str, vd: float, budget: Budget, *, threads: int | None = None, slots: int | None = None,
            max_level: int | None = None, violations_log: Path | None = None,
            timeout_s: int | None = None, mesh_scale: float | None = None,
            levels: list[int] | None = None, budget_category: str = "calibration",
            snap_spacing: float | None = None, post_render=None) -> dict[str, Any]:
    attempts = []
    store = Path(store).resolve()
    for level in recovery_levels(levels, max_level):
        r = D.render(templates, spec, values, device, vd, level=level, threads=threads)
        if mesh_scale:
            r = D.Rendered(sde=D.refine_mesh(r.sde, mesh_scale), sdevice=r.sdevice, par=r.par, values=r.values)
        if post_render is not None:
            r = post_render(r)
        if snap_spacing:   # on-request diagnostic run: denser snapshots, still on the 37.5 mV current-plot grid
            r = D.Rendered(sde=r.sde, sdevice=dense_snapshots(r.sdevice, snap_spacing),
                           par=r.par, values=r.values)
        mesh = ensure_mesh(store, r.sde, device, violations_log)
        _check_frozen_mesh(mesh, device, mesh_scale, level, violations_log)
        support_md5 = {n: read_json(Path(store) / "support_md5.json", {}).get(n) for n in C.SYNOPSYS_SUPPORT_FILES}
        key = sha256_text("\n\x00".join([mesh["mesh_hash"], r.sdevice, r.par, str(support_md5), C.SENTAURUS_VERSION]))[:16]
        rdir = Path(store) / "sim" / key
        status_path = rdir / "run_status.json"
        with file_lock(Path(store) / "sim" / f"{key}.lock"):
            prev = read_json(status_path)
            if prev is not None:
                prev["cache_hit"] = True
                attempts.append(prev)
                if prev["ok"] or prev.get("syntax_error"):
                    break
                continue
            rdir.mkdir(parents=True, exist_ok=True)
            (rdir / "sdevice_idvg.cmd").write_text(r.sdevice)
            (rdir / "sdevice_iwo_process.par").write_text(r.par)
            for name in C.SYNOPSYS_SUPPORT_FILES:
                dst = rdir / name
                if not dst.exists():
                    os.symlink(Path(store) / "support" / name, dst)
            if not (rdir / "n1_msh.tdr").exists():
                os.symlink(mesh["tdr"], rdir / "n1_msh.tdr")
            n_used = budget.take(f"{device} VD={vd:g} level={level} {key}", budget_category)
            with cpu_slot(C.GLOBAL_SLOT_DIR, C.GLOBAL_SLOTS), cpu_slot(Path(store) / ".slots", slots or C.CPU_SLOTS):
                res = _run([str(C.SENTAURUS_BIN / "sdevice"), "sdevice_idvg.cmd"], rdir, "sdevice_capture.log",
                           (timeout_s or C.SDEVICE_TIMEOUT_S) * max(C.RECOVERY_LEVELS[level].get("TIMEOUT_FACTOR", 1),
                                                       2 if mesh_scale and mesh_scale < 1 else 1))
            ok, reason, npts = False, "", 0
            plts = sorted(rdir.glob("iwo_IdVg_off*.plt"))
            extra: dict[str, Any] = {}
            if res["status"] == "success" and plts:
                try:
                    df = parse_plt(plts[0])
                    df.to_csv(rdir / "idvg.csv", index=False)
                    ok, reason = _sweep_ok(df)
                    npts = len(df)
                except Exception as exc:  # noqa: BLE001
                    reason = f"parse failure: {exc}"
                if ok:
                    extra = post_process(rdir, plts[0], device)
                    if not extra["current_balance"]["ok"]:
                        ok = False
                        cb = extra["current_balance"]
                        reason = (f"false convergence: |I_D+I_S+I_G|/|I_D| = {cb['max_rel']:.2e} ({cb['max_dec']:.4f} dec) at "
                                  f"V_G {cb['at_VG']:.3f} V, above {C.BALANCE_MAX_DEC} dec")
            else:
                full = (rdir / "sdevice_capture.log").read_text(errors="ignore")
                kind, detail = classify_failure(full, res["status"])
                reason = f"sdevice {res['status']} ({kind}{': ' + detail if detail else ''})"
                if kind == "deck error before any solve":
                    reason += "; recovery skipped"
            for big in list(rdir.glob("*.sav")) + list(rdir.glob("*_des.tdr")):
                big.unlink()
            stored = sum(f.stat().st_size for f in rdir.iterdir() if f.is_file() and not f.is_symlink())
            if not extra and (rdir / "n1_des.log").exists():
                try:
                    extra = {"echo": _echo_summary((rdir / "n1_des.log").read_text(errors="ignore"))}
                except Exception:  # noqa: BLE001
                    extra = {}
            eps_iwo = D.iwo_par_epsilon(Path(store) / "support" / C.CHANNEL_PAR)
            hf = D.par_material_blocks(r.par)[C.GATE_DIELECTRIC]
            status = {
                "ok": ok, "reason": reason, "run_key": key, "run_dir": str(rdir), "device": device,
                "sample": C.DEVICES[device]["sample"], "geometry": C.DEVICES[device]["geometry"], "vd": vd,
                "recovery_level": level, "recovery_name": C.RECOVERY_LEVELS[level]["name"],
                "sentaurus": C.SENTAURUS_VERSION, "threads": threads or C.THREADS, "sdevice": res,
                "n_points": npts, "mesh_hash": mesh["mesh_hash"], "mesh_readback": mesh["readback"],
                C.K_GD_KEY: sorted({float(x) for x in re.findall(r"epsilon\s*=\s*([-+0-9.eE]+)", hf)}),
                C.K_CH_KEY: sorted(set(eps_iwo)), "values": r.values, "budget_used_after": n_used,
                "utc": now_utc(), "cache_hit": False,
                "mesh_scale": mesh_scale,
                "validity": "VALID" if ok else ("FALSE_CONVERGENCE" if reason.startswith("false convergence") else "SOLVER_FAILED"),
                **{k: v for k, v in extra.items() if k in ("current_balance", "echo", "internal_file", "snapshots_file",
                                                            C.CH_MAT_KEY, "n_snapshots", "snapshot_error", "postproc_s")},
                "stored_bytes": stored,
            }
            status["syntax_error"] = (not ok) and "deck error before any solve" in reason
            write_json(status_path, status)
            attempts.append(status)
            if ok or status["syntax_error"]:
                break
    final = attempts[-1]
    return {**final, "attempts": [{"run_key": a["run_key"], "ok": a["ok"], "reason": a["reason"],
                                   "level": a["recovery_level"], "cache_hit": a.get("cache_hit", False)} for a in attempts]}


def _echo_summary(text: str) -> dict[str, Any]:
    from . import verify as V
    e = V.log_echo(text)
    return {"electrodes": e["electrodes"], "bgn": e["bgn"], "global": e["global"],
            "locations": {k: {"traps": v["traps"], "lines": v["lines"][:40]} for k, v in e["locations"].items()},
            "parameters": e["parameters"]}


def post_process(rdir: Path, plt: Path, device: str) -> dict[str, Any]:
    import gzip
    import json as _json
    from . import verify as V
    t0 = time.time()
    out: dict[str, Any] = {}
    full = V.parse_plt_all(plt)
    out["current_balance"] = V.current_balance(full)
    keep = [c for c in full.columns if c.startswith(("Pos(", "Integr")) or c in (
        "gate OuterVoltage", "drain OuterVoltage", "gate Charge", "drain TotalCurrent", "source TotalCurrent",
        "gate TotalCurrent")]
    full[keep].to_csv(rdir / "internal.csv", index=False)
    out["internal_file"] = "internal.csv"
    log = rdir / "n1_des.log"
    out["echo"] = _echo_summary(log.read_text(errors="ignore")) if log.exists() else {}
    snaps = sorted(rdir.glob("n1_snap_*_des.tdr")) or sorted(rdir.glob("n1_0*_des.tdr"))
    if snaps:
        try:
            cuts, mode = _extract_all(snaps, device)
            cuts = [{**c, "device": device} for c in cuts]
            out["snapshot_extraction"] = mode
            phi0 = cuts[0].get("phi_gate_boundary_V")
            for c in cuts:   # V_G of each snapshot: the first one is the start of the gate sweep (VG_SIM_START)
                if phi0 is not None and c.get("phi_gate_boundary_V") is not None:
                    c["VG_V"] = round(C.VG_SIM_START + (c["phi_gate_boundary_V"] - phi0), 6)
            out[C.CH_MAT_KEY] = cuts[0].get(C.CH_MAT_KEY)
            with gzip.open(rdir / "snapshots.json.gz", "wt") as fh:
                _json.dump(_compact_snapshots(cuts), fh)
            out["snapshots_file"], out["n_snapshots"] = "snapshots.json.gz", len(cuts)
        except Exception as exc:  # noqa: BLE001
            out["snapshot_error"] = f"{type(exc).__name__}: {exc}"
    out["postproc_s"] = round(time.time() - t0, 2)
    return out


def _extract_all(snaps: list[Path], device: str) -> tuple[list[dict[str, Any]], str]:
    from functools import partial
    f = partial(MESH.extract_snapshot, device=device)
    n = min(C.SNAPSHOT_WORKERS, len(snaps))
    if n > 1:
        try:
            import multiprocessing as mp
            with cf.ProcessPoolExecutor(n, mp_context=mp.get_context("spawn")) as ex:
                return list(ex.map(f, snaps)), f"parallel ({n} processes)"
        except (OSError, RuntimeError, cf.process.BrokenProcessPool):
            pass
    return [f(t) for t in snaps], "sequential"


def _compact_snapshots(cuts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Columnar, rounded form of the snapshot cuts (about 30-40 kB per snapshot before compression)."""
    def col(rows: list[dict[str, Any]]) -> dict[str, list]:
        keys = sorted({k for r in rows for k in r})
        return {k: [r.get(k) if not isinstance(r.get(k), float) else float(f"{r[k]:.6g}") for r in rows] for k in keys}
    out = []
    for c in cuts:
        lat = {}
        for name, rows in c["lateral"].items():   # full resolution within 0.15 um of an inner edge, every 3rd point elsewhere
            lat[name] = col([r for k, r in enumerate(rows) if k % 3 == 0 or _near_edge(r["x_um"], c.get("device"))])
        out.append({"VG_V": c.get("VG_V"), "tdr": c["tdr"], "phi_gate_boundary_V": c.get("phi_gate_boundary_V"),
                    C.CH_MAT_KEY: c.get(C.CH_MAT_KEY),
                    "vertical": {k: col(v) for k, v in c["vertical"].items()}, "lateral": lat})
    return out


def _near_edge(x: float, device: str | None = None) -> bool:
    g = C.GEOMETRIES[C.DEVICES[device]["geometry"] if device in C.DEVICES else "L1_lov1p5"]
    return min(abs(x - g["Lov_each_um"]), abs(x - g["Lov_each_um"] - g["L_um"])) < 0.15


def simulate(store: Path, templates: Mapping[str, str], spec: Mapping[str, Any], values: Mapping[str, float],
             conditions: list[tuple[str, float]], budget: Budget, **kw: Any) -> list[dict[str, Any]]:
    with cf.ThreadPoolExecutor(max_workers=len(conditions)) as pool:
        futs = [pool.submit(run_one, store, templates, spec, values, dev, vd, budget, **kw) for dev, vd in conditions]
        out = []
        for f in futs:
            try:
                out.append(f.result())
            except (BudgetExhausted, GeometryViolation, D.DeckGuardError) as exc:
                out.append({"ok": False, "reason": f"{type(exc).__name__}: {exc}", "fatal": type(exc).__name__})
        return out


def load_curve(run: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    df = pd.read_csv(Path(run["run_dir"]) / "idvg.csv")
    return df["Vg"].to_numpy(float), df["Id"].to_numpy(float)


def init_store(store: Path) -> dict[str, str]:
    store = Path(store).resolve()
    store.mkdir(parents=True, exist_ok=True)
    with file_lock(store / ".init_store.lock"):
        rec = read_json(store / "support_md5.json")
        if rec and all((store / "support" / n).exists() and D.md5_file(store / "support" / n) == rec.get(n)
                       for n in C.SYNOPSYS_SUPPORT_FILES):
            return rec
        md5 = D.copy_support_files(store / "support")
        write_json(store / "support_md5.json", md5)
        return md5
