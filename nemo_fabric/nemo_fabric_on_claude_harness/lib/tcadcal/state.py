"""Workspace state: parameter registry, deck versions, accepted card, history, gates.
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any, Mapping

from . import config as C
from . import deck as D
from .util import append_jsonl, now_utc, read_json, read_jsonl, sha256_file, write_json

START_PARAMS = {
    "WF_gate": {"token": "@WF_gate@", "init": 4.4, "unit": "eV", "scale": "linear", "file": "sdevice",
                "meaning": "TCAD gate work function (Phi_G,TCAD): the one effective gate-alignment parameter of the "
                           f"{C.MATERIALS['gate']} gate, set by the alignment; it may absorb electrostatic offsets the model does not resolve"},
    "WF_sd": {"token": "@WF_sd@", "init": 4.7, "unit": "eV", "scale": "linear", "file": "sdevice",
              "meaning": "source/drain Schottky contact work function (Phi_SD)"},
    "Nd_eff": {"token": "@Nd_eff@", "init": 1.0e18, "unit": "cm^-3", "scale": "log", "file": "sde",
               "meaning": f"uniform effective donor density of the {C.CHANNEL} channel (N_D,eff)"},
    "mu0": {"token": "@mu0@", "init": 10.0, "unit": "cm^2/Vs", "scale": "linear", "file": "par",
            "meaning": f"constant electron mobility of {C.CHANNEL} (mu0)"},
}


class StateError(RuntimeError):
    pass


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        marker = self.root / ".tcadcal_workspace.json"
        if not marker.exists():
            raise StateError(f"{self.root} is not a calibration workspace (run tools from the workspace root)")
        self.meta = read_json(marker)

    @property
    def program(self) -> str:
        return self.meta.get("program", "main")

    @property
    def objective(self) -> str:
        return self.meta.get("objective", "J_objective")

    @property
    def alignment_loss(self) -> str | None:
        return self.meta.get("alignment_loss")

    @property
    def calibration(self) -> list:
        return C.calibration(self.program)

    def rule(self, name: str) -> bool:
        return bool((self.meta.get("rules") or {}).get(name))

    @property
    def fixed(self) -> dict[str, float]:
        meta = read_json(self.root / ".tcadcal_workspace.json") or self.meta
        return {k: float(v) for k, v in (meta.get("fixed_params") or {}).items()}

    # paths
    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def store(self) -> Path:
        return self.root / "runs"

    @property
    def harness_dir(self) -> Path:
        return self.root.parent / "harness_state"

    def deck_dir(self, version: str) -> Path:
        return self.state / "decks" / version

    def log(self, kind: str, **payload: Any) -> None:
        append_jsonl(self.state / "history.jsonl", {"utc": now_utc(), "kind": kind, **payload})

    # registry
    def registry(self) -> dict[str, Any]:
        return read_json(self.state / "registry.json")

    def save_registry(self, reg: Mapping[str, Any]) -> None:
        write_json(self.state / "registry.json", reg)

    def deck_versions(self) -> list[str]:
        return sorted(p.name for p in (self.state / "decks").iterdir() if p.is_dir())

    def params_for(self, version: str) -> dict[str, Any]:
        """Registry entries whose tokens are used by deck ``version``."""
        reg = self.registry()["params"]
        used = set().union(*(D.tokens_in(D.strip_comments(t)) for t in D.read_templates(self.deck_dir(version)).values()))
        return {k: v for k, v in reg.items() if v["token"].strip("@") in used}

    def accepted(self) -> dict[str, Any]:
        return read_json(self.state / "accepted.json")

    def base_values(self, version: str) -> dict[str, float]:
        """Accepted values, plus the initial values of parameters new in ``version``."""
        acc = self.accepted()
        vals = {}
        for name, spec in self.params_for(version).items():
            vals[name] = acc["values"].get(name, spec["init"])
        return vals

    def bounds_ready(self, names: list[str]) -> list[str]:
        reg = self.registry()["params"]
        return [n for n in names if n not in self.fixed and (reg[n].get("lo") is None or reg[n].get("hi") is None)
                and not (n == "WF_gate" and self.alignment_loss in C.GATE_FREE_LOSSES)]


# ---------------------------------------------------------------------------
# Scaled coordinates (x in [0, 1] within the approved bounds; log for densities)
# ---------------------------------------------------------------------------
def to_unit(spec: Mapping[str, Any], value: float) -> float:
    lo, hi = spec["lo"], spec["hi"]
    if spec["scale"] == "log":
        return (math.log10(value) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
    return (value - lo) / (hi - lo)


def from_unit(spec: Mapping[str, Any], x: float) -> float:
    lo, hi = spec["lo"], spec["hi"]
    x = min(max(x, 0.0), 1.0)
    if spec["scale"] == "log":
        return 10 ** (math.log10(lo) + x * (math.log10(hi) - math.log10(lo)))
    return lo + x * (hi - lo)


def apply_reviewer_fix(ws: Workspace, pid: str, names: list[str]) -> dict[str, Any]:
    marker = ws.root / ".tcadcal_workspace.json"
    meta = read_json(marker)
    ref = (meta.get("reference") or {}).get("values") or {}
    reg = ws.registry()
    bad = [n for n in names if n not in ref or n not in reg["params"]]
    if bad:
        raise StateError(f"cannot fix {bad}: not parameters of the reference card")
    vals = {n: float(ref[n]) for n in names}
    meta["fixed_params"] = {**(meta.get("fixed_params") or {}), **vals}
    meta["rules"] = {**(meta.get("rules") or {}), "fixed_work_functions": True, "align_start_last": False}
    meta.setdefault("rule_changes", []).append({"utc": now_utc(), "by_review": pid, "fixed_params": vals})
    write_json(marker, meta)
    ws.meta = meta
    for n, v in vals.items():
        reg["params"][n].update({"fixed": v, "lo": None, "hi": None, "basis": f"fixed by the reviewer at {pid}"})
    ws.save_registry(reg)
    acc = ws.accepted()
    changed = {n: [acc["values"].get(n), v] for n, v in vals.items() if acc["values"].get(n) != v}
    if changed:
        acc = {"deck": acc["deck"], "values": {**acc["values"], **vals}, "J": None, "eval": None, "utc": now_utc(),
               "calibration_target_met": None, "source": None,
               "note": f"reset at review {pid}: {sorted(vals)} fixed at the reference values, baseline to be evaluated again"}
        write_json(ws.state / "accepted.json", acc)
    (ws.state / "last_alignment.json").unlink(missing_ok=True)
    ws.log("reviewer_fix", proposal=pid, fixed=vals, changed=changed)
    note = (f"{', '.join(f'{n} = {v:g}' for n, v in vals.items())} fixed at the reference values (run rule "
            "fixed_work_functions): no alignment, no bounds, no probes or fitting from now on.")
    if changed:
        note += " The accepted card was reset to these values and its J cleared: evaluate it again and accept it as the baseline."
    return {"fixed": vals, "changed": changed, "note": note}


def init_workspace_state(root: Path, cap: int | None, start: Mapping[str, Any] | None = None) -> None:
    """``start`` (optional) starts from a reference model instead of the Step-0 deck: {"deck_dir": the reference
    deck version, "params": its registry entries, "values": its final card, "note": text}. The bounds are cleared,
    so the agent proposes them again for the new device (G1)."""
    root = Path(root)
    st = root / "state"
    (st / "decks").mkdir(parents=True, exist_ok=True)
    v0 = st / "decks" / "v000"
    if v0.exists():
        raise StateError("state already initialised")
    v0.mkdir()
    src = Path(start["deck_dir"]) if start else D.DECK0
    for f in D.FILES.values():
        shutil.copy2(src / f, v0 / f)
    if start:
        params = {k: {kk: vv for kk, vv in v.items() if kk not in ("lo", "hi", "basis", "since", "bounds_proposal")}
                  for k, v in start["params"].items()}
        params = {k: {**v, "init": float(start["values"][k]), "lo": None, "hi": None, "basis": None, "since": "v000"}
                  for k, v in params.items()}
        note = start["note"]
    else:
        params = {k: {**v, "lo": None, "hi": None, "basis": None, "since": "v000"} for k, v in START_PARAMS.items()}
        note = "start deck (device specification, no DOS)"
    reg = {"params": params,
           "decks": {"v000": {"parent": None, "proposal": None, "utc": now_utc(), "hash": D.deck_hash(v0), "note": note}}}
    write_json(st / "registry.json", reg)
    write_json(st / "accepted.json", {"deck": "v000", "values": {k: v["init"] for k, v in params.items()},
                                      "J": None, "eval": None, "utc": now_utc()})
    write_json(st / "budget.json", {"used": 0, "cap": cap})
    append_jsonl(st / "history.jsonl", {"utc": now_utc(), "kind": "init", "deck": "v000"})


# ---------------------------------------------------------------------------
# Loop bookkeeping: step records, acceptance with the mechanism check, stall, model-addition rule
# ---------------------------------------------------------------------------
TARGET_KEYS = {"V_th": "dVth", "SS": "dSS", "SS_profile": "dSS_profile", "I_on": "dlogIon", "E_lin": "E_lin"}
SHIFT_KEY = "V_th_shift"
REGION_KEYS = ("subthreshold", "turn_on", "on", "off")
RECORD_TEXT_FIELDS = ("physical_reading", "action", "plausibility", "remaining")


def _branch_for(ev: Mapping[str, Any], vd: float) -> Mapping[str, Any]:
    for b in ev["branches"].values():
        if abs(float(b["vd"]) - vd) < 1e-9:
            return b
    raise StateError(f"no curve at V_D = {vd} V in {ev['eval_id']}")


def target_value(ev: Mapping[str, Any], item: str) -> float:
    if item == SHIFT_KEY:
        sh = ev.get("V_th_shift")
        if not sh:
            raise StateError("this evaluation has no V_th shift (it needs both calibration curves)")
        return abs(float(sh["error"]))
    if "@" not in item:
        raise StateError(f"targeted item {item!r} needs '@<V_D>' (e.g. SS@0.05), or is '{SHIFT_KEY}'")
    what, vd = item.split("@", 1)
    b = _branch_for(ev, float(vd))
    if what.startswith("region:"):
        reg = what.split(":", 1)[1]
        if reg not in REGION_KEYS:
            raise StateError(f"unknown region {reg}; use one of {REGION_KEYS}")
        if ev.get("objective") in C.TERM_OBJECTIVES and reg in (b.get("curve_terms") or {}):
            return float(b["curve_terms"][reg]["value"])
        v = b["regions"][reg].get("mean_r2")
        if v is None:
            raise StateError(f"region {reg} has no points at V_D = {vd} V")
        return float(v)
    if what not in TARGET_KEYS:
        raise StateError(f"unknown metric {what}; use one of {sorted(TARGET_KEYS)}, {SHIFT_KEY} or region:<name>")
    return abs(float(b["metric_errors"][TARGET_KEYS[what]]))


OBS_LOCATIONS = ("source contact", "source edge", "channel", "drain edge", "drain contact", "gate interface",
                 "back interface", "film", "not localized")
OBS_BIAS = ("off", "subthreshold", "threshold", "on")


def check_internal_observation(ws: "Workspace", obs: Any, where: str, full: bool = True) -> list[str]:
    """An observation of the simulated device interior, tied to a stored evaluation and a bias. It is a model prediction,
    never evidence that a mechanism is right. ``full`` (physics additions, final model) also needs the location and the
    decision it supports. A parameter step may cite the relevant summary values only, or state that the interior
    diagnostics identify no additional mechanism."""
    if not isinstance(obs, Mapping):
        return [f"{where}: required, an object {{eval, V_D, bias, quantity, observation, is_model_prediction"
                + (", location, supports" if full else "") + "}} read from the evaluation's inside_the_device summary"]
    errs = []
    ev = read_json(ws.root / "evals" / f"{obs.get('eval')}.json") if obs.get("eval") else None
    if not ev:
        errs.append(f"{where}.eval: {obs.get('eval')!r} is not a stored evaluation")
    elif not ev.get("bands"):
        errs.append(f"{where}.eval: {obs.get('eval')} has no inside_the_device summary")
    if str(obs.get("V_D")) not in {*C.VD_KEYS, *(str(v) for v in C.CAL_VDS)}:
        errs.append(f"{where}.V_D: {C.VD_KEYS[0]} or {C.VD_KEYS[1]}")
    bias = obs.get("bias")
    if not (bias in OBS_BIAS or isinstance(bias, (int, float))):
        errs.append(f"{where}.bias: one of {OBS_BIAS} or a V_G value")
    if (full or obs.get("location") is not None) and obs.get("location") not in OBS_LOCATIONS:
        errs.append(f"{where}.location: one of {OBS_LOCATIONS}")
    for f, n in ((("quantity", 5), ("observation", 30), ("supports", 20)) if full else (("quantity", 5), ("observation", 15))):
        if len(str(obs.get(f, "")).strip()) < n:
            errs.append(f"{where}.{f}: >= {n} characters" + (" (which decision this observation informs)" if f == "supports" else ""))
    if obs.get("is_model_prediction") is not True:
        errs.append(f"{where}.is_model_prediction must be true: internal quantities are predictions of the simulated "
                    "model, not measurements or evidence for a mechanism")
    return errs


AT_BOUND_FIELDS = (("limited_terms", 5), ("compensation", 10), ("independent_basis_for_extension", 4))


def check_at_bound_response(ab: Any, at_b: list[str]) -> list[str]:
    """A bound reached is a diagnostic to explain, not a reason to widen the range. The record says
    which J terms the bound limits, which other parameters compensate, and any independent basis for a wider range."""
    if isinstance(ab, Mapping) and all(len(str(ab.get(k, "")).strip()) >= n for k, n in AT_BOUND_FIELDS):
        return []
    return [f"parameters at a bound {at_b}: the step record needs 'at_bound_response' {{limited_terms: which J terms the "
            "bound limits, compensation: which other parameters moved to make up for it, independent_basis_for_extension: "
            "an independent source for a wider range, or 'none'}. A bound reached is a finding to explain, not by itself "
            "a reason to widen the range"]


def validate_record(rec: Mapping[str, Any], first: bool, ws: "Workspace | None" = None) -> list[str]:
    errs = []
    for f in RECORD_TEXT_FIELDS:
        if len(str(rec.get(f, "")).strip()) < 15:
            errs.append(f"step record field '{f}' must state it in device terms (>= 15 characters)")
    if not first and not rec.get("targeted"):
        errs.append("step record needs 'targeted': the residual(s) this step is meant to reduce, e.g. ['SS@0.05', 'SS@1']")
    for item in rec.get("targeted") or []:
        if not isinstance(item, str) or (item != SHIFT_KEY and "@" not in item):
            errs.append(f"targeted item {item!r}: use '<metric>@<V_D>', 'region:<name>@<V_D>' or '{SHIFT_KEY}'")
    if ws is not None and ws.rule("step_evidence") and not first:
        from . import evidence
        errs += evidence.check(ws, rec.get("evidence"), "step record 'evidence'")
    if ws is not None and ws.rule("band_reading"):
        errs += check_internal_observation(ws, rec.get("internal_observation"), "step record 'internal_observation'",
                                           full=False)
    return errs


# Jobs whose evaluations may be accepted after the baseline: optimizer points, probe points (fixed tool steps)
# and the smoke test of an approved patch at its approved initial values. A plain `evaluate` job carries values
# typed by the agent and is a diagnostic only.
ACCEPT_SOURCES = ("dfols", "ofat", "smoke")


def job_kind(ws: Workspace, job: str | None) -> str:
    spec = read_json(ws.root / "jobs" / str(job) / "spec.json") if job else None
    return (spec or {}).get("kind") or "unknown"

def accept(ws: Workspace, ev: Mapping[str, Any], reason: str, record: Mapping[str, Any] | None = None,
           source: str | None = None) -> dict[str, Any]:
    acc = ws.accepted()
    first = acc["J"] is None
    if not ev.get("converged"):
        raise StateError(f"eval {ev['eval_id']} did not converge at every bias; it cannot be accepted")
    validity = ev.get("validity")
    if validity is not None and validity != "VALID":
        hint = {"OUT_OF_ENVELOPE": f"its parameters lie outside the numerically verified range of deck {ev['deck']}. Verify "
                                   f"the card on the refined mesh first: python3 tools/verify.py {ev['eval_id']}",
                "MESH_SENSITIVE": f"deck {ev['deck']} changed beyond the numerical criteria on the refined mesh; this is a "
                                  "numerical finding, not evidence about the physics, and it goes to the reviewer",
                "FALSE_CONVERGENCE": "a run violated the terminal-current balance (false convergence)"}.get(validity, "")
        raise StateError(f"eval {ev['eval_id']} has validity {validity}; it cannot be accepted. {hint}")
    inactive = {k: a.get("why") for k, a in (ev.get("activation") or {}).items() if a.get("label") == "INACTIVE"}
    if inactive:
        raise StateError(f"eval {ev['eval_id']}: deck parameters not applied by Sentaurus (log echo): {inactive}. The deck does "
                         "not implement the approved model; correct the patch")
    if ev["deck"] not in ws.deck_versions():
        raise StateError(f"unknown deck version {ev['deck']}")
    if not first:
        kind = job_kind(ws, ev.get("job"))
        if kind not in ACCEPT_SOURCES:
            raise StateError(f"eval {ev['eval_id']} comes from a '{kind}' job, i.e. values set by hand. Accepted values must "
                             f"come from the tools ({', '.join(ACCEPT_SOURCES)}). Use a hand-set evaluation as a diagnostic "
                             "only, and record what it showed in the journal")
    record = dict(record or {})
    errs = validate_record(record, first, ws)
    if ws.rule("band_reading"):
        spec0 = ws.params_for(ev["deck"])
        at_b = [n for n, sp in spec0.items() if sp.get("lo") is not None and n != "WF_gate" and n in ev["values"] and
                (abs(to_unit(sp, ev["values"][n])) < 1e-3 or abs(to_unit(sp, ev["values"][n]) - 1) < 1e-3)]
        al = ev.get("alignment") or {}
        win = al.get("window_V") or [None, None]
        sh = al.get("shift_V")
        if sh is not None and win[0] is not None and win[0] > -C.ALIGN_WINDOW_V + 1e-6 and abs(sh - win[0]) < 1e-3:
            at_b.append("WF_gate (alignment stopped at the approved lower bound)")
        if sh is not None and win[1] is not None and win[1] < C.ALIGN_WINDOW_V - 1e-6 and abs(sh - win[1]) < 1e-3:
            at_b.append("WF_gate (alignment stopped at the approved upper bound)")
        if at_b:
            errs += check_at_bound_response(record.get("at_bound_response"), at_b)
    if errs:
        raise StateError("; ".join(errs))
    check = {}
    if not first:
        if not ev["J"] <= acc["J"] - C.ACCEPT_MIN_DJ:
            raise StateError(f"J {ev['J']:.5g} does not improve the accepted J {acc['J']:.5g} by >= {C.ACCEPT_MIN_DJ}")
        prev = load_eval_record(ws, acc["eval"])
        for item in record["targeted"]:
            before, after = target_value(prev, item), target_value(ev, item)
            check[item] = {"before": round(before, 4), "after": round(after, 4), "reduced": after < before}
        failed = [k for k, v in check.items() if not v["reduced"]]
        if failed:
            raise StateError(f"mechanism check failed: the targeted residual(s) {failed} did not shrink {check}. "
                             "Record the trial with `state reject` and its physical reading")
    spec = ws.params_for(ev["deck"])
    at_bound = [n for n, sp in spec.items() if sp.get("lo") is not None and n != "WF_gate" and
                (abs(to_unit(sp, ev["values"][n])) < 1e-3 or abs(to_unit(sp, ev["values"][n]) - 1) < 1e-3)]
    prev_j = acc["J"]
    new = {"deck": ev["deck"], "values": ev["values"], "J": ev["J"], "eval": ev["eval_id"], "utc": now_utc(),
           "calibration_target_met": ev.get("calibration_target_met"), "source": source}
    write_json(ws.state / "accepted.json", new)
    gain = None if prev_j is None else (prev_j - ev["J"]) / prev_j
    steps = sorted((ws.state / "steps").glob("S*.json")) if (ws.state / "steps").exists() else []
    sid = f"S{len(steps) + 1:03d}"
    from .jobs import shift_snapshot, snapshot  # local import: jobs imports state
    step = {"step": sid, "utc": now_utc(), "eval": ev["eval_id"], "deck": ev["deck"], "J_before": prev_j, "J_after": ev["J"],
            "rel_gain": gain, "metrics_before": snapshot(load_eval_record(ws, acc["eval"])) if not first else None,
            "metrics_after": snapshot(ev), "shift_before": shift_snapshot(load_eval_record(ws, acc["eval"])) if not first else None,
            "shift_after": shift_snapshot(ev), "targeted": record.get("targeted", []), "mechanism_check": check,
            "physical_reading": record.get("physical_reading"), "action": record.get("action"),
            "plausibility": record.get("plausibility"), "remaining": record.get("remaining"),
            "evidence": record.get("evidence"), "internal_observation": record.get("internal_observation"),
            "at_bound_response": record.get("at_bound_response"),
            "values": ev["values"], "parameters_at_bound": at_bound, "reason": reason}
    write_json(ws.state / "steps" / f"{sid}.json", step)
    append_step_log(ws, step)
    ws.log("accept", eval=ev["eval_id"], deck=ev["deck"], J_before=prev_j, J_after=ev["J"], rel_gain=gain, step=sid,
           reason=reason, source=source)
    return {"accepted": ev["eval_id"], "step": sid, "J_before": prev_j, "J_after": ev["J"], "rel_gain": gain,
            "mechanism_check": check, "parameters_at_bound": at_bound}


def load_eval_record(ws: Workspace, eval_id: str) -> dict[str, Any]:
    ev = read_json(ws.root / "evals" / f"{eval_id}.json")
    if ev is None:
        raise StateError(f"no evaluation {eval_id}")
    return ev


def append_step_log(ws: Workspace, step: Mapping[str, Any]) -> None:
    lines = [f"\n## {step['step']} ({step['eval']}, deck {step['deck']}, {step['utc']})",
             f"- J: {step['J_before']} -> {step['J_after']:.4g}"]
    for vd, m in step["metrics_after"].items():
        b = (step["metrics_before"] or {}).get(vd)
        def fmt(k):
            a = m[k]
            if a is None:
                return "n/a"
            return f"{b[k]:.3g} -> {a:.3g}" if b and b[k] is not None else f"{a:.3g}"
        lines.append(f"- {vd}: dV_th {fmt('dV_th_mV')} mV, dSS {fmt('dSS_percent')} %, dlog I_on {fmt('dlog10_I_on_dec')} dec, "
                     f"E_lin {fmt('E_lin')}")
    sa, sb = step.get("shift_after"), step.get("shift_before")
    if sa and sa.get("error_mV") is not None:
        before = f"{sb['error_mV']:+.1f} -> " if sb and sb.get("error_mV") is not None else ""
        lines.append(f"- V_th shift (TCAD - measured): {before}{sa['error_mV']:+.1f} mV (tolerance {sa['tolerance_mV']:.1f} mV)")
    lines += [f"- Targeted: {', '.join(step['targeted']) or '(baseline)'}  mechanism check: {step['mechanism_check']}",
              f"- Physical reading: {step['physical_reading']}", f"- Action: {step['action']}",
              f"- Plausibility: {step['plausibility']}" + (f" (at bound: {step['parameters_at_bound']})" if step['parameters_at_bound'] else ""),
              f"- Remaining: {step['remaining']}"]
    if step.get("evidence"):
        from . import evidence
        lines.append(f"- Evidence: {evidence.brief(step['evidence'])}")
    p = ws.state / "STEP_LOG.md"
    if not p.exists():
        p.write_text("# Calibration step log (written by the tools from the agent's step records)\n")
    with open(p, "a") as fh:
        fh.write("\n".join(lines) + "\n")


def dfols_results(ws: Workspace) -> list[dict[str, Any]]:
    return [h for h in read_jsonl(ws.state / "history.jsonl") if h["kind"] == "dfols_result"]


def activation_status(ws: Workspace, accepted_eval: Mapping[str, Any] | None) -> dict[str, Any]:
    acc = ws.accepted()
    last = [h for h in dfols_results(ws) if h.get("deck") == acc["deck"]]
    stall = bool(last and last[-1].get("stall_confirmed"))
    override = [h for h in read_jsonl(ws.state / "history.jsonl")
                if h["kind"] == "user_override" and h.get("deck") == acc["deck"] and h.get("model_addition_allowed")]
    numerically_limited = bool(last and last[-1].get("stall_numerically_limited"))
    target_met = bool(accepted_eval and accepted_eval.get("calibration_target_met"))
    outside = []
    secondary_outside = []
    if accepted_eval and accepted_eval.get("objective") in C.TERM_OBJECTIVES:
        for k, v in (accepted_eval.get("J_terms") or {}).items():
            if v > 1.0:
                outside.append(f"{k} (term {v:.2f})")
    elif accepted_eval:
        for b in accepted_eval["branches"].values():
            for k, v in b["metric_errors"].items():
                if abs(v) > 1.0:
                    outside.append(f"{k}@{b['vd']:g} ({v:+.2f} tolerances)")
            for reg in ("subthreshold", "turn_on", "on"):
                r = b["regions"].get(reg, {})
                if r.get("mean_r2") is not None and r["mean_r2"] > 1.0 and r.get("same_sign_fraction", 0) >= 0.8:
                    outside.append(f"region:{reg}@{b['vd']:g} (mean r2 {r['mean_r2']:.2f}, same sign)")
    out_note = ("the last fit stalled with numerical failures near its best point (or < 90 % converged points). That is a "
                "numerical limitation, not evidence that the deck lacks physics: no model addition is opened by it"
                if numerically_limited else "")
    return {"model_addition_allowed": (stall or bool(override)) and not target_met, "parameter_update_stalled": stall,
            "user_override": override[-1].get("reason") if override else None,
            "stall_numerically_limited": numerically_limited, "note": out_note,
            "last_dfols_job": last[-1]["job"] if last else None, "calibration_target_met": target_met,
            "residuals_outside_tolerance": outside,
            **({"secondary_terms_outside_tolerance": secondary_outside} if secondary_outside else {})}
