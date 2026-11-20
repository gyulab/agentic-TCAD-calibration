"""One J evaluation of a parameter card on the two calibration anchors.

An evaluation = render the deck version with the card, build/verify the mesh, run sdevice at
the two calibration drain biases (in parallel), score J and the metrics, write ``evals/E#####.json`` and an
overlay plot. Evaluations are cached by (deck hash, rendered values).
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from . import config as C
from . import deck as D
from . import measurements as M
from . import objective as O
from . import sentaurus as S
from .state import Workspace
from .util import append_jsonl, file_lock, now_utc, read_json, sha256_text, write_json

_PREPS: dict[str, list[O.MeasPrep]] = {}


def calibration_preps(program: str = "main") -> list[O.MeasPrep]:
    if program not in _PREPS:
        _PREPS[program] = [O.prepare(M.load_forward(dev, vd), C.ss_low(program)) for dev, vd in C.calibration(program)]
    return _PREPS[program]


def alignment_start(ws: Workspace, values: Mapping[str, float]) -> dict[str, float]:
    return alignment_start_record(ws, values)[0]


def alignment_start_record(ws: Workspace, values: Mapping[str, float]) -> tuple[dict[str, float], dict[str, Any] | None]:
    if not ws.rule("align_start_last") or "WF_gate" in ws.fixed or "WF_gate" not in values:
        return dict(values), None
    near = nearest_alignment_record(ws, values)
    if near is not None:
        used, source, rule = near[0], near[1], "nearest converged aligned evaluation (run rule align_start_last)"
    else:
        last = read_json(ws.state / "last_alignment.json")
        if not last:
            return dict(values), None
        used, source, rule = float(last["WF_gate_aligned_eV"]), last.get("eval"), "last aligned evaluation (run rule align_start_last)"
    req = float(values["WF_gate"])
    rec = {"param": "WF_gate", "requested": req, "used": used, "changed": abs(used - req) > 1e-12, "rule": rule,
           "source_eval": source,
           "reason": "the gate work function is aligned after every evaluation; the start only sets where the search "
                     "begins, and the aligned result is the same exact V_G shift from any start"}
    return {**values, "WF_gate": used}, rec


def _param_distance(a: Mapping[str, float], b: Mapping[str, float], reg: Mapping[str, Any]) -> float | None:
    """Squared distance in bound-normalised units (log10 for log-scale parameters), WF_gate excluded."""
    d = 0.0
    for k in a:
        if k == "WF_gate":
            continue
        spec = reg.get(k) or {}
        x, y, lo, hi = float(a[k]), float(b[k]), spec.get("lo"), spec.get("hi")
        if spec.get("scale") == "log":
            if x <= 0 or y <= 0:
                return None
            span = math.log10(hi / lo) if lo and hi and hi > lo > 0 else 1.0
            d += ((math.log10(x) - math.log10(y)) / span) ** 2
        else:
            span = hi - lo if lo is not None and hi is not None and hi > lo else max(abs(y), 1e-12)
            d += ((x - y) / span) ** 2
    return d


def nearest_alignment(ws: Workspace, values: Mapping[str, float]) -> float | None:
    """Aligned WF_gate of the converged evaluation with the same parameter set that lies nearest to ``values``."""
    near = nearest_alignment_record(ws, values)
    return near[0] if near else None


def nearest_alignment_record(ws: Workspace, values: Mapping[str, float]) -> tuple[float, str | None] | None:
    """(aligned WF_gate, evaluation id) of the nearest converged evaluation with the same parameter set."""
    path = ws.root / "logs" / "evals.jsonl"
    if not path.exists():
        return None
    reg = ws.registry().get("params") or {}
    keys = {k for k in values if k != "WF_gate"}
    best: tuple[float, float, str | None] | None = None
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        v = r.get("values") or {}
        if r.get("status") != "ok" or r.get("J") is None or r.get("aligned") is False or "WF_gate" not in v \
                or set(v) - {"WF_gate"} != keys:
            continue
        d = _param_distance(values, v, reg)
        if d is not None and (best is None or d < best[0]):
            best = (d, float(v["WF_gate"]), r.get("eval"))
    return (best[1], best[2]) if best else None


def _next_id(ws: Workspace, prefix: str, sub: str) -> str:
    d = ws.root / sub
    d.mkdir(parents=True, exist_ok=True)
    with file_lock(d / ".id.lock"):
        cnt = read_json(d / ".counter.json", {"n": 0})
        cnt["n"] += 1
        write_json(d / ".counter.json", cnt)
    return f"{prefix}{cnt['n']:05d}"


def evaluate(ws: Workspace, deck_version: str, values: Mapping[str, float], *, label: str = "",
             job: str | None = None, budget: S.Budget | None = None) -> dict[str, Any]:
    spec = ws.params_for(deck_version)
    missing = [k for k in spec if k not in values]
    if missing:
        raise ValueError(f"values missing for {missing}")
    for name, sp in spec.items():
        v = float(values[name])
        if sp.get("lo") is not None and not (sp["lo"] * (1 - 1e-9) <= v <= sp["hi"] * (1 + 1e-9)):
            raise ValueError(f"{name} = {v:g} outside the approved bounds [{sp['lo']:g}, {sp['hi']:g}]")
        if not math.isfinite(v) or (sp["scale"] == "log" and v <= 0):
            raise ValueError(f"invalid value {name} = {v}")
    for name, v in ws.fixed.items():
        if name in spec and abs(float(values[name]) - v) > 1e-9:
            raise ValueError(f"{name} is fixed at {v:g} (run rule fixed_work_functions), got {float(values[name]):g}")
    templates = D.read_templates(ws.deck_dir(deck_version))
    cal = list(ws.calibration)
    requested = {k: float(v) for k, v in values.items()}
    values, start_rec = alignment_start_record(ws, values)
    rounded = D.render(templates, spec, values, cal[0][0], cal[0][1]).values
    value_changes = [start_rec] if start_rec and start_rec["changed"] else []
    for name, w in rounded.items():
        if name == "WF_gate" and value_changes:
            continue
        if name in requested and w != requested[name]:
            value_changes.append({"param": name, "requested": requested[name], "used": w, "changed": True,
                                  "rule": "deck formatting", "source_eval": None,
                                  "reason": "deck text precision (util.fmt_value: 6 significant digits, 7 in e-notation for log-scale values)"})
    # an evaluation without alignment (fixed gate work function) is a different result for the same values
    key = sha256_text(D.deck_hash(ws.deck_dir(deck_version)) + repr(sorted(rounded.items()))
                      + ("|fixed_wf" if "WF_gate" in ws.fixed else ""))[:20]
    index_path = ws.root / "evals" / "index.json"
    with file_lock(ws.root / "evals" / ".index.lock"):
        idx = read_json(index_path, {})
        if key in idx:
            ev = read_json(ws.root / "evals" / f"{idx[key]}.json")
            if ev and ev.get("complete"):
                return {**ev, "cache_hit": True}
    budget = budget or S.Budget(ws.state / "budget.json", None)
    t0 = now_utc()
    runs = S.simulate(ws.store, templates, spec, rounded, cal, budget,
                      violations_log=ws.root / "logs" / "violations.jsonl", timeout_s=ws.meta.get("sdevice_timeout_s"))
    eval_id = _next_id(ws, "E", "evals")
    ev: dict[str, Any] = {"eval_id": eval_id, "label": label, "job": job, "deck": deck_version,
                          "deck_hash": D.deck_hash(ws.deck_dir(deck_version)), "values": rounded,
                          "values_requested": requested, "value_changes": value_changes,
                          "started_utc": t0, "finished_utc": now_utc(), "complete": True, "cache_key": key,
                          "runs": [_run_view(r) for r in runs]}
    fatal = [r.get("fatal") for r in runs if r.get("fatal")]
    ev["converged"] = all(r.get("ok") for r in runs)
    if fatal:
        ev.update({"J": None, "status": "fatal", "fatal": fatal})
    elif not ev["converged"] and all(r.get("deck_error") for r in runs if not r.get("ok")):
        ev.update({"J": None, "status": "deck_error", "note": DECK_ERROR_NOTE})
    elif not ev["converged"]:
        ev.update({"J": None, "status": "numerical_failure",
                   "note": "excluded from model-update decisions (recovery sequence exhausted)"})
    else:
        sims = [S.load_curve(r) for r in runs]
        final_runs = runs
        ref = float(rounded["WF_gate"])
        wf = spec.get("WF_gate", {})
        recenter, recenter_failed = [], []
        reaches_vth = all(np.nanmax(np.abs(np.asarray(i, float))) >= C.VTH_LEVEL_A for _, i in sims)
        # Run rule fixed_work_functions: the gate work function is a fixed input, so no alignment is made
        aligns = reaches_vth and "WF_gate" not in ws.fixed
        sims_first, ref_first = sims, ref
        al_stats, al_t0 = {"n_score": 0}, time.process_time()
        while aligns:
            lo = -C.ALIGN_WINDOW_V if wf.get("lo") is None else max(-C.ALIGN_WINDOW_V, wf["lo"] - ref)
            hi = C.ALIGN_WINDOW_V if wf.get("hi") is None else min(C.ALIGN_WINDOW_V, wf["hi"] - ref)
            d, sc = O.align(calibration_preps(ws.program), sims, lo, hi, objective=ws.objective, loss=ws.alignment_loss,
                            stats=al_stats)
            at_window = (abs(d - C.ALIGN_WINDOW_V) < 1e-3 and hi >= C.ALIGN_WINDOW_V - 1e-9) or \
                        (abs(d + C.ALIGN_WINDOW_V) < 1e-3 and lo <= -C.ALIGN_WINDOW_V + 1e-9)
            if not at_window or len(recenter) >= C.ALIGN_MAX_RECENTER:
                break
            # The best shift lies beyond the simulated sweep: re-simulate with the gate work function moved
            # to the window edge and align again (the alignment must be exact, not clipped).
            new_ref = float(D.render(templates, spec, {**rounded, "WF_gate": ref + d}, cal[0][0],
                                     cal[0][1]).values["WF_gate"])
            runs = S.simulate(ws.store, templates, spec, {**rounded, "WF_gate": new_ref}, cal, budget,
                              violations_log=ws.root / "logs" / "violations.jsonl", timeout_s=ws.meta.get("sdevice_timeout_s"))
            if not all(r.get("ok") for r in runs):
                recenter_failed.append(new_ref)
                break
            ref = new_ref
            recenter.append(ref)
            final_runs = runs
            sims = [S.load_curve(r) for r in runs]
        al_cpu_s = round(time.process_time() - al_t0, 3)
        if not aligns:
            # no crossing: no shift, at the gate value the evaluation started from
            d, sc = 0.0, O.score(calibration_preps(ws.program), sims, ws.objective)
            lo = hi = 0.0
        if final_runs is not runs or recenter:
            ev["runs_first_simulation"] = ev["runs"]
            ev["runs"] = [_run_view(r) for r in final_runs]
        rounded = {**rounded, "WF_gate": ref}
        aligned = dict(rounded)
        aligned["WF_gate"] = round(ref + d, 5)
        br = O.brief(sc)
        fixed_wf = None
        if aligns:
            sc0 = O.score(calibration_preps(ws.program), sims, ws.objective)
            b0 = O.brief(sc0)
            fixed_wf = {"WF_gate_eV": ref, "J": sc0["J"], "J_terms": b0.get("J_terms"),
                        "V_th_shift_error": None if not sc0.get("V_th_shift") else sc0["V_th_shift"]["error"],
                        "note": "the simulated curves at the rendered gate work function, without the alignment shift"}
            fixed_wf["residual_vector_unweighted"] = sc0["residual_vector_unweighted"]
        first_fixed_wf = None
        if aligns and recenter:
            sf = O.score(calibration_preps(ws.program), sims_first, ws.objective)
            bf = O.brief(sf)
            first_fixed_wf = {"WF_gate_eV": ref_first, "J": sf["J"], "J_terms": bf.get("J_terms"),
                              "V_th_shift_error": None if not sf.get("V_th_shift") else sf["V_th_shift"]["error"],
                              "residual_vector": sf["residual_vector"],
                              "residual_vector_unweighted": sf["residual_vector_unweighted"],
                              "note": "the first simulation (before re-centring) at its rendered gate work function, "
                                      "without the alignment shift"}
        ev.update({"status": "ok", "objective": sc["objective"], "J": sc["J"],
                   "J_terms": sc["terms"], "V_th_shift": sc["V_th_shift"],
                   "calibration_target_met": sc["target"],
                   "values_rendered": rounded, "values": aligned,
                   "alignment": {"WF_gate_rendered_eV": ref, "shift_V": round(d, 5), "window_V": [round(lo, 4), round(hi, 4)],
                                 "loss": ws.alignment_loss or ws.objective, "cpu_s": al_cpu_s,
                                 "n_scorings": al_stats["n_score"], "n_recentring_attempts": len(recenter) + len(recenter_failed),
                                 "at_bound": _bound_status(aligns, d, lo, hi),
                                 "at_window_edge": bool(aligns and min(abs(d - lo), abs(d - hi)) < 1e-3),
                                 "recentered_WF_gate_eV": recenter, "recentering_failed_at_WF_gate_eV": recenter_failed,
                                 "defined": bool(aligns), "fixed_work_function": "WF_gate" in ws.fixed},
                   "summary": br, "residual_vector": sc["residual_vector"],
                   "residual_vector_unweighted": sc["residual_vector_unweighted"],
                   "at_rendered_gate_work_function": fixed_wf,
                   **({"at_first_rendered_gate_work_function": first_fixed_wf} if first_fixed_wf else {}),
                   "branches": {k: {kk: vv for kk, vv in b.items() if kk != "curve"} for k, b in sc["branches"].items()},
                   "curves": {k: b["curve"] for k, b in sc["branches"].items()}})
        try:
            plot_eval(ws.root / "evals" / f"{eval_id}.png", ev, sc)
            ev["plot"] = f"evals/{eval_id}.png"
        except Exception as exc:  # noqa: BLE001
            ev["plot_error"] = str(exc)
    ev.update(annotate(ws, deck_version, templates, spec, ev, locals().get("final_runs") or runs))
    if ev.get("status") == "ok" and ws.rule("band_reading"):
        try:
            from . import bands as BD
            t0 = time.time()
            feats = D.model_features(templates["sdevice"])
            bulk = any(f.endswith(":Traps") and f.split(":")[0] in (f"Material={C.CHANNEL}", f"Region={C.REGIONS['channel']}") for f in feats)
            ev["bands"] = BD.summary(ev, bulk_traps=bulk)
            ev["bands_s"] = round(time.time() - t0, 2)
            art = (ev["bands"] or {}).get("contact_edge_artifact") or {}
            if art.get("flag"):
                ev.setdefault("validity_notes", []).append(
                    f"artifact screen: conduction-band spike of {art['spike_eV']} eV above both neighbours at x = "
                    f"{art.get('x_um')} um (inner contact edge, V_G {art.get('V_G')} V, V_D {art.get('V_D')} V)")
        except Exception as exc:  # noqa: BLE001
            ev["bands_error"] = f"{type(exc).__name__}: {exc}"
    write_json(ws.root / "evals" / f"{eval_id}.json", ev)
    if ws.rule("align_start_last") and ev.get("status") == "ok" and (ev.get("alignment") or {}).get("defined"):
        write_json(ws.state / "last_alignment.json", {"eval": eval_id, "WF_gate_aligned_eV": ev["values"]["WF_gate"]})
    with file_lock(ws.root / "evals" / ".index.lock"):
        idx = read_json(index_path, {})
        if ev["status"] in ("ok", "numerical_failure", "deck_error"):
            idx[key] = eval_id
        write_json(index_path, idx)
    append_jsonl(ws.root / "logs" / "evals.jsonl", {"utc": now_utc(), "eval": eval_id, "job": job, "deck": deck_version,
                                                    "values": ev.get("values", rounded), "J": ev.get("J"), "status": ev["status"],
                                                    "aligned": bool((ev.get("alignment") or {}).get("defined"))})
    return ev


def _bound_status(aligns: bool, d: float, lo: float, hi: float) -> str | None:
    """'lower' / 'upper' when the alignment stopped at an approved WF_gate bound (not at the +-0.3 V window edge)."""
    if not aligns:
        return None
    if lo > -C.ALIGN_WINDOW_V + 1e-6 and abs(d - lo) < 1e-3:
        return "lower"
    if hi < C.ALIGN_WINDOW_V - 1e-6 and abs(d - hi) < 1e-3:
        return "upper"
    return None


def _run_view(r: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: r.get(k) for k in ("ok", "reason", "run_key", "device", "vd", "recovery_level", "recovery_name", "n_points",
                                 "mesh_hash", "cache_hit", "sdevice", "attempts", "fatal", C.K_GD_KEY, C.K_CH_KEY, "mesh_readback",
                                 "validity", "current_balance", C.CH_MAT_KEY, "n_snapshots", "snapshot_error",
                                 "run_dir", "postproc_s", "stored_bytes")}
    if r.get("deck_error"):
        out["deck_error"] = r["deck_error"]
    return out


DECK_ERROR_NOTE = ("sdevice stopped while reading the command file of this deck version, before any solve: the deck is "
                   "invalid and no recovery level was tried (runs: deck_error). The model of this deck version is not "
                   "applied. A deck change needs a corrected model_patch proposal.")


def envelope_path(ws: Workspace) -> Path:
    legacy = ws.state / "envelope.json"
    return legacy if legacy.exists() else ws.harness_dir / "envelope.json"


def envelope(ws: Workspace) -> dict[str, Any]:
    return read_json(envelope_path(ws), {}) or {}


def deck_flags(ws: Workspace, deck_version: str) -> dict[str, Any]:
    return envelope(ws).get(deck_version, {})


def annotate(ws: Workspace, deck_version: str, templates: Mapping[str, str], spec: Mapping[str, Any],
             ev: Mapping[str, Any], runs: list[Mapping[str, Any]]) -> dict[str, Any]:
    from . import inside as INS
    from . import verify as V
    out: dict[str, Any] = {}
    vals = {r.get("validity") for r in runs}
    if ev.get("status") == "fatal":
        validity = "FATAL"
    elif ev.get("status") == "deck_error":
        validity = V.DECK_ERROR
    elif V.FALSE_CONVERGENCE in vals and not ev.get("converged"):
        validity = V.FALSE_CONVERGENCE
    elif not ev.get("converged"):
        validity = V.SOLVER_FAILED
    else:
        validity = V.VALID
    flags = deck_flags(ws, deck_version)
    notes = []
    if validity == V.VALID and ws.rule("numerical_envelope"):
        if flags.get("mesh_sensitive"):
            validity = V.MESH_SENSITIVE
            notes.append(f"deck {deck_version} was found mesh-sensitive: {flags['mesh_sensitive']}")
        else:
            cards = [c["values"] for c in flags.get("verified", [])]
            ok, miss = V.within_envelope(ev.get("values_rendered") or ev.get("values") or {}, spec, cards)
            if not ok:
                validity = V.OUT_OF_ENVELOPE
                notes += miss
    elif validity == V.VALID and ws.rule("fixed_mesh"):
        cards = [c["values"] for c in flags.get("verified", [])]
        ok, miss = V.within_envelope(ev.get("values_rendered") or ev.get("values") or {}, spec, cards)
        out["envelope_flag"] = {"within_verified_range": ok, "outside": miss}
    out["validity"] = validity
    out["validity_notes"] = notes
    # activation from the log echo of the first converged run (field read-back: channel doping of the mesh)
    run0 = next((r for r in runs if r.get("ok")), runs[0] if runs else None)
    if run0 and run0.get("run_dir"):
        rs = read_json(Path(run0["run_dir"]) / "run_status.json") or {}
        echo = rs.get("echo")
        if echo:
            try:
                rb = (run0.get("mesh_readback") or {})
                out["activation"] = V.activation(templates, spec, ev.get("values_rendered") or ev.get("values"), echo, rb)
            except Exception as exc:  # noqa: BLE001
                out["activation_error"] = f"{type(exc).__name__}: {exc}"
    inside = {}
    snaps = {}
    for r in runs:
        if r.get("ok") and r.get("run_dir") and (Path(r["run_dir"]) / "internal.csv").exists():
            try:
                inside[f"{r['vd']:g}"] = INS.summarize(Path(r["run_dir"]) / "internal.csv", r["device"], float(r["vd"]))
            except Exception as exc:  # noqa: BLE001
                inside[f"{r['vd']:g}"] = {"error": f"{type(exc).__name__}: {exc}"}
            if (Path(r["run_dir"]) / "snapshots.json.gz").exists():
                snaps[f"{r['vd']:g}"] = str(Path(r["run_dir"]) / "snapshots.json.gz")
    if inside:
        out["inside"] = inside
    if snaps:
        out["snapshots"] = snaps
    return out


def refresh_validity(ws: Workspace, ev: dict[str, Any]) -> dict[str, Any]:
    """Re-derive the envelope part of the validity after a verification (the card may now be verified)."""
    from . import verify as V
    if ev.get("validity") not in (V.OUT_OF_ENVELOPE, V.VALID, V.MESH_SENSITIVE) or not ws.rule("numerical_envelope"):
        return ev
    flags = deck_flags(ws, ev["deck"])
    spec = ws.params_for(ev["deck"])
    if flags.get("mesh_sensitive"):
        ev.update({"validity": V.MESH_SENSITIVE})
    else:
        ok, miss = V.within_envelope(ev.get("values_rendered") or ev["values"], spec,
                                     [c["values"] for c in flags.get("verified", [])])
        ev.update({"validity": V.VALID if ok else V.OUT_OF_ENVELOPE, "validity_notes": miss})
    write_json(ws.root / "evals" / f"{ev['eval_id']}.json", ev)
    return ev


def load_eval(ws: Workspace, eval_id: str) -> dict[str, Any]:
    ev = read_json(ws.root / "evals" / f"{eval_id}.json")
    if ev is None:
        raise FileNotFoundError(f"no evaluation {eval_id}")
    return ev


def eval_view(ev: Mapping[str, Any]) -> dict[str, Any]:
    """What the agent sees: no per-point arrays, but the region residual table."""
    keys = ("eval_id", "label", "deck", "values", "status", "J", "calibration_target_met", "alignment", "converged",
            "plot")
    out = {k: ev.get(k) for k in keys}
    if ev.get("summary"):
        for k in ("J_terms", "V_th_shift"):
            if k in ev["summary"]:
                out[k] = ev["summary"][k]
        out["curves"] = ev["summary"]["curves"]
    out["runs"] = [{**{k: r.get(k) for k in ("device", "vd", "ok", "reason", "recovery_name", "cache_hit", "validity")},
                    **({"deck_error": r["deck_error"]} if r.get("deck_error") else {})} for r in ev["runs"]]
    if ev.get("status") == "deck_error":
        out["note"] = ev.get("note")
    if ev.get("validity"):
        out["validity"] = ev["validity"]
        if ev.get("validity_notes"):
            out["validity_notes"] = ev["validity_notes"]
    if ev.get("activation"):
        out["activation"] = {k: v["label"] for k, v in ev["activation"].items()}
        bad = {k: v["why"] for k, v in ev["activation"].items() if v["label"] in ("INACTIVE", "ACTIVATION_UNVERIFIED")}
        if bad:
            out["activation_notes"] = bad
    if ev.get("bands"):
        # Pushed, not on request (model predictions from the stored snapshots and the per-bias table)
        out["inside_the_device"] = ev["bands"]
        out["band_diagrams"] = f"python3 tools/diagnose.py --bands {ev['eval_id']} --vd 1"
    elif ev.get("inside"):
        out["inside_the_device"] = "python3 tools/diagnose.py --show " + ev["eval_id"]
    if ev.get("fatal"):
        out["fatal"] = ev["fatal"]
    return out


def plot_eval(path: Path, ev: Mapping[str, Any], sc: Mapping[str, Any]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(3, 2, figsize=(9, 9.5), sharex=True)
    for j, (k, b) in enumerate(sc["branches"].items()):
        c = b["curve"]
        vg, im, isim = np.asarray(c["vg"]), np.abs(np.asarray(c["i_meas"])), np.asarray(c["i_sim"])
        ax[0, j].semilogy(vg, np.maximum(im, 1e-14), "ko", ms=3, label="measured (forward)")
        ax[0, j].semilogy(vg, isim, "r-", lw=1.5, label="TCAD")
        ax[0, j].axhline(b["loq_A"], color="0.6", ls=":", lw=1, label="LOQ")
        ax[0, j].set_title(f"V_D = {b['vd']:g} V  curve terms " + ", ".join(
            f"{r} {t['value']:.2g}" for r, t in b["curve_terms"].items()), fontsize=9)
        ax[0, j].set_ylabel("|I_D| (A)")
        ax[0, j].legend(fontsize=7, loc="lower right")
        ax[1, j].plot(vg, np.asarray(c["i_meas"]) * 1e6, "ko", ms=3)
        ax[1, j].plot(vg, isim * 1e6, "r-", lw=1.5)
        ax[1, j].set_ylabel("I_D (uA)")
        ax[2, j].bar(vg, np.asarray(c["r"]), width=0.04, color="C0")
        ax[2, j].axhline(1, color="0.5", ls=":", lw=1)
        ax[2, j].axhline(-1, color="0.5", ls=":", lw=1)
        ax[2, j].set_ylabel("r_i (tolerance units)")
        ax[2, j].set_xlabel("V_G (V)")
    out = [k for k, v in sc["terms"].items() if v > 1.0]
    sh = sc.get("V_th_shift") or {}
    shift = "" if sh.get("TCAD_mV") is None or sh.get("measured_mV") is None else \
        f"\nV_th shift TCAD {sh['TCAD_mV']:.0f} mV vs measured {sh['measured_mV']:.0f} mV"
    fig.suptitle(f"{ev['eval_id']}  deck {ev['deck']}  J = {sc['J']:.4g}  target {'met' if sc['target'] else 'not met'}"
                 f" ({len(out)} of {sc['n_terms']} terms outside tolerance){shift}", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)
