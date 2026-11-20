"""
Proposal types:
  bounds       G1: parameter bounds with their basis
  model_patch  G-model: a deck patch that adds physics, with new parameters, evidence and expected effect
  final        G-final: freeze the accepted card
"""
from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from . import config as C
from . import deck as D
from .state import StateError, Workspace, activation_status, target_value
from .util import append_jsonl, now_utc, read_json, read_jsonl, sha256_file, write_json

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
TYPES = ("bounds", "model_patch", "final")


def proposal_path(ws: Workspace, pid: str) -> Path:
    if not re.fullmatch(r"P\d{3}", pid):
        raise StateError("proposal ids look like P001")
    return ws.root / "proposals" / f"{pid}.json"


def _need(d: dict, keys: list[str], where: str, errs: list[str]) -> None:
    for k in keys:
        if d.get(k) in (None, "", []):
            errs.append(f"{where}: missing '{k}'")


CANDIDATE_FIELDS = ["rank", "model_class", "mechanism", "predicted_effect", "evidence", "decision", "reason"]
DESCRIPTOR_FIELDS = ["name", "value", "physical_meaning", "resolved_residual", "literature_comparison",
                     "constraint_class", "constraint_evidence"]
CONSTRAINT_CLASSES = ("well constrained", "weakly constrained", "not constrained")
REFCMP_FIELDS = ["name", "reference_value", "final_value", "change", "physical_meaning", "resolved_residual",
                 "literature_comparison", "evidence", "constraint_class"]

DIRECTIONS = ("down", "up", "both")


def _last_residual_utc(ws: Workspace, kinds: tuple[str, ...]) -> str:
    """Time of the latest residual the agent had to read (an accepted evaluation or a finished DFO-LS)."""
    from .util import read_jsonl
    hist = [h for h in read_jsonl(ws.state / "history.jsonl") if h.get("kind") in kinds]
    return max((h["utc"] for h in hist), default="")


def _searches_after(ws: Workspace, t0: str) -> int:
    """Literature (WebSearch/WebFetch, hook log) and manual (kb_search, tool log) searches after t0."""
    from .util import read_jsonl
    web = [w for w in read_jsonl(ws.root / "logs" / "web.jsonl") if w.get("tool") in C.WEB_TOOL_KIND]
    kb = [t for t in read_jsonl(ws.root / "logs" / "tool_events.jsonl") if t.get("tool") == "kb_search"]
    return sum(1 for e in web + kb if str(e.get("utc", "")) > t0)


def _physics_order(ws: Workspace, prop: dict, kind: str, kb, errs: list[str]) -> None:
    from . import evidence
    t0 = _last_residual_utc(ws, ("accept",) if kind == "bounds" else ("accept", "dfols_result"))
    if _searches_after(ws, t0) == 0:
        errs.append(f"physics_order: no literature or manual search after the latest residual ({t0 or 'none'}). Read the "
                    "residual, write the physical reading, search the literature and the manual for that reading, then propose")
    if kind != "bounds":
        return
    if len(str(prop.get("residual", ""))) < 20:
        errs.append("physics_order: 'residual' must state which metrics or regions are outside tolerance, at which V_D, "
                    "with numbers (the residual this proposal answers)")
    ev = prop.get("evidence") or {}
    if not (ev.get("manual") and ev.get("literature")):
        errs.append("physics_order: 'evidence' needs at least one Sentaurus manual chunk and one literature note, each with "
                    "a verbatim quote, found for this physical reading")
    errs += evidence.check(ws, ev, "physics_order.evidence", kb, min_items=2)
    ref = ((ws.meta.get("reference") or {}).get("values")) or {}
    if not ref:
        ref = {n: p.get("init") for n, p in (ws.registry().get("params") or {}).items()}
    for name, spec in (prop.get("bounds") or {}).items():
        if name in ws.fixed or not isinstance(spec, dict):
            continue
        where = f"bounds.{name}"
        if len(str(spec.get("reading", ""))) < 20:
            errs.append(f"{where}: 'reading' must give the physical reading of the residual for this parameter")
        if name == "WF_gate" and ws.alignment_loss in C.GATE_FREE_LOSSES:
            errs.append("bounds.WF_gate: the gate offset is set by the gate alignment after every simulation and has no bounds")
            continue
        if name == "WF_gate" and ws.rule("align_start_last"):
            continue        # the gate alignment is not a search parameter: its bounds need a basis, not a direction
        d = spec.get("direction")
        if d not in DIRECTIONS:
            errs.append(f"{where}: 'direction' must be one of {DIRECTIONS}: the direction your reading expects for this "
                        "device relative to the reference value (the start value in a run from the start deck)")
            continue
        r, lo, hi = ref.get(name), spec.get("lo"), spec.get("hi")
        if r is None or not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            continue
        if d == "down" and hi > r * (1 + 1e-9):
            errs.append(f"{where}: your reading expects a value below the reference {r:g}, so the upper bound must not exceed "
                        f"it (hi {hi:g}). Use direction 'both' with 'both_evidence' if a source supports an increase")
        if d == "up" and lo < r * (1 - 1e-9):
            errs.append(f"{where}: your reading expects a value above the reference {r:g}, so the lower bound must not be "
                        f"below it (lo {lo:g}). Use direction 'both' with 'both_evidence' if a source supports a decrease")
        if d == "both":
            errs += evidence.check(ws, spec.get("both_evidence"), f"{where}.both_evidence", kb, min_items=1)


def validate(ws: Workspace, pid: str, accepted_eval: dict | None = None, kb=None) -> dict[str, Any]:
    path = proposal_path(ws, pid)
    if not path.exists():
        raise StateError(f"{path.relative_to(ws.root)} not found")
    try:
        prop = read_json(path)
    except Exception as exc:  # noqa: BLE001
        raise StateError(f"{path.name} is not valid JSON: {exc}")
    errs: list[str] = []
    if prop.get("id") != pid:
        errs.append(f"'id' must be {pid}")
    typ = prop.get("type")
    if typ not in TYPES:
        errs.append(f"'type' must be one of {TYPES}")
    _need(prop, ["title", "rationale"], "proposal", errs)
    if typ == "model_patch" and ws.rule("parameters_only"):
        errs.append("model_patch: outside the scope of this run. This run recalibrates the declared channel parameters "
                    "of the reference model (run rule parameters_only); model changes are not part of it")
    reg = ws.registry()["params"]
    acc = ws.accepted()
    if typ == "bounds":
        b = prop.get("bounds") or {}
        if not b:
            errs.append("bounds: empty")
        preset = ws.meta.get("preset_bounds") or {}
        hist = read_jsonl(ws.root / "proposals" / f"{pid}.validation_history.jsonl")
        prev_errs = (hist[-1].get("errors") if hist else None) or []
        for name, spec in b.items():
            if name not in reg:
                errs.append(f"bounds: unknown parameter {name}")
                continue
            if name == "WF_gate" and ws.alignment_loss in C.GATE_FREE_LOSSES:
                errs.append("bounds.WF_gate: the gate offset is set by the gate alignment after every simulation "
                            "and has no bounds (inputs/objective.md); leave it out of the proposal")
                continue
            if name in preset:
                errs.append(f"bounds.{name}: set by the run configuration at [{preset[name]['lo']:g}, "
                            f"{preset[name]['hi']:g}] ({preset[name]['basis']}); leave it out of the proposal")
                continue
            if name in ws.fixed:
                errs.append(f"bounds.{name}: fixed at {ws.fixed[name]:g} (run rule fixed_work_functions), give it no bounds")
                continue
            _need(spec, ["lo", "hi", "basis"], f"bounds.{name}", errs)
            lo, hi = spec.get("lo"), spec.get("hi")
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
                if not lo < hi:
                    errs.append(f"bounds.{name}: lo must be < hi")
                if reg[name]["scale"] == "log" and lo <= 0:
                    errs.append(f"bounds.{name}: log-scaled parameter needs lo > 0")
                v = acc["values"].get(name, reg[name]["init"])
                if not lo <= v <= hi:
                    res = spec.get("start_resolution")
                    ok_res = (isinstance(res, Mapping) and isinstance(res.get("new_start"), (int, float))
                              and lo <= float(res["new_start"]) <= hi and len(str(res.get("reason", "")).strip()) >= 20)
                    if not ok_res:
                        errs.append(f"bounds.{name}: the current (start) value {v:g} lies outside [{lo:g}, {hi:g}]. Do not "
                                    "widen the bounds only to include it. Resolve it explicitly: either keep these bounds "
                                    "and add 'start_resolution': {'new_start': <value inside the bounds>, 'reason': <why>} "
                                    "(the start value then moves, the change is recorded and the baseline is evaluated "
                                    "again), or change the bounds on a physical basis and state the change in "
                                    "'bounds_change_reason'")
                elif any(e.startswith(f"bounds.{name}: the current (start) value") for e in prev_errs) and \
                        len(str(spec.get("bounds_change_reason", "")).strip()) < 20:
                    errs.append(f"bounds.{name}: the previous version had the start value outside its bounds, and these "
                                "bounds now include it. State why in 'bounds_change_reason' (>= 20 characters, a physical "
                                "basis, not only that the start value must fit), or keep the earlier bounds with a "
                                "'start_resolution'")
            if len(str(spec.get("basis", ""))) < 20:
                errs.append(f"bounds.{name}: basis must state the source of the range")
        unset = [n for n, s in reg.items() if s.get("lo") is None and n not in b and n not in ws.fixed
                 and not (n == "WF_gate" and ws.alignment_loss in C.GATE_FREE_LOSSES)]
        if unset:
            errs.append(f"bounds: parameters still without bounds: {unset}")
        if ws.rule("physics_order"):
            _physics_order(ws, prop, "bounds", kb, errs)
    elif typ == "model_patch":
        if ws.rule("physics_order"):
            _physics_order(ws, prop, "model_patch", kb, errs)
        act = activation_status(ws, accepted_eval)
        if not act["model_addition_allowed"]:
            errs.append(f"physics-model addition not allowed yet: {act}")
        dfc = prop.get("deficiency") or {}
        _need(dfc, ["targeted", "measured_vs_TCAD", "physical_reading", "why_existing_parameters_cannot_fix", "probe_job"],
              "deficiency", errs)
        for item in dfc.get("targeted") or []:
            try:
                if accepted_eval is not None and target_value(accepted_eval, item) <= 1.0 and not item.startswith("region:"):
                    errs.append(f"deficiency.targeted {item} is already within tolerance on the accepted card")
            except StateError as exc:
                errs.append(f"deficiency.targeted: {exc}")
        if ws.rule("internal_reading"):
            io = dfc.get("internal_observation") or {}
            if not io.get("eval") or not io.get("observation"):
                errs.append("deficiency.internal_observation: {eval, observation} is required: one inside-the-device "
                            "observation (python3 tools/diagnose.py --show/--bands) on the accepted model that locates "
                            "where the deficient quantity sits in the simulated device")
            else:
                ev = read_json(ws.root / "evals" / f"{io['eval']}.json")
                if not ev or not ev.get("inside"):
                    errs.append(f"deficiency.internal_observation.eval {io['eval']} has no inside-the-device record")
                if io.get("is_model_prediction") is not True:
                    errs.append("deficiency.internal_observation.is_model_prediction must be true: internal quantities are "
                                "predictions of the simulated model, not measurements or evidence")
        if ws.rule("band_reading"):
            # A physics addition reads one band diagram of the accepted model at the targeted bias
            bd = dfc.get("band_diagram") or {}
            fig = str(bd.get("figure", ""))
            if not bd.get("eval") or not fig:
                errs.append("deficiency.band_diagram: {eval, V_D, figure, observation, is_model_prediction} is required: run "
                            "python3 tools/diagnose.py --bands <eval> --vd <V_D> [--vg ...] on the accepted model and state what "
                            "the band diagram shows where the deficient quantity sits")
            else:
                if not fig.startswith(f"evals/{bd['eval']}_bands_vd") or not (ws.root / fig).exists():
                    errs.append(f"deficiency.band_diagram.figure: {fig!r} must be the figure written by "
                                f"`diagnose.py --bands {bd['eval']}` (evals/{bd['eval']}_bands_vd<V_D>.png)")
                if len(str(bd.get("observation", "")).strip()) < 40:
                    errs.append("deficiency.band_diagram.observation: >= 40 characters: what the band diagram and the "
                                "charge distribution show, with numbers")
                if len(str(bd.get("interpretation", "")).strip()) < 40:
                    errs.append("deficiency.band_diagram.interpretation: >= 40 characters: your reading of the observation, "
                                "with the alternative readings it does not exclude")
                if bd.get("is_model_prediction") is not True:
                    errs.append("deficiency.band_diagram.is_model_prediction must be true")
        pj = dfc.get("probe_job")
        if pj:
            st = read_json(ws.root / "jobs" / str(pj) / "status.json") or {}
            spec = read_json(ws.root / "jobs" / str(pj) / "spec.json") or {}
            if st.get("state") != "done" or spec.get("kind") not in ("ofat", "dfols"):
                errs.append(f"deficiency.probe_job {pj} must be a finished probe (ofat) or dfols job showing that the "
                            "existing parameters do not remove the targeted residual")
        patch = prop.get("patch") or {}
        _need(patch, ["base_deck", "edits"], "patch", errs)
        new_params = patch.get("new_params") or []
        base = patch.get("base_deck")
        if base and base not in ws.deck_versions():
            errs.append(f"patch.base_deck {base} does not exist")
        elif base:
            try:
                tpl = D.apply_edits(D.read_templates(ws.deck_dir(base)), patch.get("edits") or [])
                tokens = {v["token"].strip("@") for v in reg.values()} | {p.get("token", "").strip("@") for p in new_params}
                used = set().union(*(D.tokens_in(D.strip_comments(t)) for t in tpl.values()))
                errs += [f"deck check: {e}" for e in D.check_template(tpl, tokens & used)]
                for p in new_params:
                    tok = str(p.get("token", "")).strip("@")
                    if tok and tok not in used:
                        errs.append(f"new_params.{p.get('name')}: its token @{tok}@ appears in no deck file after the "
                                    "edits, so the parameter would have no effect on the simulation")
                unver = D.unverified_model_features(tpl, D.read_templates(ws.deck_dir("v000")))
                if unver and len(str(prop.get("activation_check") or "")) < 40:
                    errs.append(f"activation_check: the patch adds the model features {unver}. "
                                f"State, for each, the sdevice log echo or field read-back that "
                                "will show it is applied at the intended location and value")
                errs += [f"deck check: {e}" for e in D.check_fixed_tokens(tpl, ws.fixed)]
            except D.DeckGuardError as exc:
                errs.append(f"patch: {exc}")
        for p in new_params:
            _need(p, ["name", "token", "init", "lo", "hi", "scale", "unit", "basis", "meaning"], f"new_params.{p.get('name')}", errs)
            if p.get("name") in reg:
                errs.append(f"new_params: {p['name']} already registered")
            if p.get("token") != f"@{p.get('name')}@":
                errs.append(f"new_params.{p.get('name')}: token must be @{p.get('name')}@")
            if p.get("scale") not in ("log", "linear"):
                errs.append(f"new_params.{p.get('name')}: scale must be log or linear")
            try:
                if not float(p["lo"]) <= float(p["init"]) <= float(p["hi"]) or not float(p["lo"]) < float(p["hi"]):
                    errs.append(f"new_params.{p['name']}: need lo <= init <= hi and lo < hi")
                if p["scale"] == "log" and float(p["lo"]) <= 0:
                    errs.append(f"new_params.{p['name']}: log scale needs lo > 0")
            except (KeyError, TypeError, ValueError):
                pass
        ev = prop.get("evidence") or {}
        man = ev.get("manual") or []
        lit = ev.get("literature") or []
        if len(man) < 1:
            errs.append("evidence.manual: at least one Sentaurus manual chunk is required")
        for m in man:
            _need(m, ["chunk", "quote"], "evidence.manual", errs)
            if kb is not None and m.get("chunk") and kb.get(m["chunk"]) is None:
                errs.append(f"evidence.manual: unknown chunk {m.get('chunk')}")
            elif kb is not None and m.get("chunk") and m.get("quote"):
                txt = re.sub(r"\s+", " ", kb.get(m["chunk"])["text"]).lower()
                q = re.sub(r"\s+", " ", m["quote"]).lower().strip(' ."')
                if q[:60] not in txt:
                    errs.append(f"evidence.manual: quote not found verbatim in {m['chunk']} (first 60 chars must match)")
        if len(lit) < 2:
            errs.append("evidence.literature: at least two papers with DOI and quote are required")
        for l in lit:
            _need(l, ["doi", "title", "quote", "url", "supports"], "evidence.literature", errs)
            if l.get("doi") and not DOI_RE.match(str(l["doi"])):
                errs.append(f"evidence.literature: malformed DOI {l.get('doi')}")
        _need(prop, ["expected_effect"], "proposal", errs)
        cands = prop.get("candidates") or []
        if len(cands) < 2:
            errs.append("candidates: list at least two physics-model classes that could act on the targeted residual, "
                        "ranked, with the chosen one marked")
        for c in cands:
            _need(c, CANDIDATE_FIELDS, f"candidates.{c.get('model_class')}", errs)
            if c.get("decision") not in ("chosen", "rejected"):
                errs.append(f"candidates.{c.get('model_class')}: decision must be 'chosen' or 'rejected'")
        if cands:
            if sum(c.get("decision") == "chosen" for c in cands) != 1:
                errs.append("candidates: exactly one candidate must have decision 'chosen'")
            ranks = [c.get("rank") for c in cands]
            if sorted(r for r in ranks if isinstance(r, int)) != list(range(1, len(cands) + 1)):
                errs.append("candidates: ranks must be 1..n without gaps")
            elif next((c.get("rank") for c in cands if c.get("decision") == "chosen"), None) != 1:
                errs.append("candidates: the chosen candidate must have rank 1")
    elif typ == "final":
        _need(prop, ["eval", "summary"], "final", errs)
        if prop.get("eval") and prop["eval"] != acc.get("eval"):
            errs.append(f"final.eval must be the accepted evaluation {acc.get('eval')}")
        ins = prop.get("insight") or {}
        _need(ins, ["descriptors", "device_conclusions", "limits", "follow_up"], "insight", errs)
        if ws.rule("band_reading"):
            from .state import check_internal_observation
            errs += _interior_check(ws, acc, ins.get("interior_check"))
            errs += _gate_alignment(ws, acc, ins.get("gate_alignment"))
        named = {d.get("name") for d in ins.get("descriptors") or []}
        missing = [n for n in ws.params_for(acc["deck"]) if n != "WF_gate" and n not in ws.fixed and n not in named] \
            if acc.get("deck") else []
        if missing:
            errs.append(f"insight.descriptors: missing {missing} (every deck parameter except the aligned gate work function)")
        for d in ins.get("descriptors") or []:
            _need(d, DESCRIPTOR_FIELDS, f"insight.descriptors.{d.get('name')}", errs)
            if d.get("constraint_class") not in CONSTRAINT_CLASSES:
                errs.append(f"insight.descriptors.{d.get('name')}: constraint_class must be one of {CONSTRAINT_CLASSES}")
        if ws.rule("reference_comparison"):
            errs += _reference_comparison(ws, ins, acc, kb)
    out = {"id": pid, "type": typ, "ok": not errs, "errors": errs, "sha256": sha256_file(path), "utc": now_utc()}
    write_json(ws.root / "proposals" / f"{pid}.validated.json", out)
    record_validation(ws, pid, path, out)
    return out


def _numeric_leaves(obj: Any, prefix: str = "") -> dict[str, float]:
    """Every numeric value of a proposal by its JSON path (bounds, initial values, any other number)."""
    if isinstance(obj, bool):
        return {}
    if isinstance(obj, (int, float)):
        return {prefix: float(obj)}
    out: dict[str, float] = {}
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            out.update(_numeric_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = v.get("name") if isinstance(v, Mapping) and v.get("name") else str(i)
            out.update(_numeric_leaves(v, f"{prefix}[{key}]"))
    return out


def record_validation(ws: Workspace, pid: str, path: Path, out: Mapping[str, Any]) -> dict[str, Any]:
    pdir = ws.root / "proposals"
    hdir = pdir / "history"
    hdir.mkdir(parents=True, exist_ok=True)
    copy = hdir / f"{pid}.{out['sha256'][:12]}.json"
    if not copy.exists():
        copy.write_bytes(path.read_bytes())
    hist = pdir / f"{pid}.validation_history.jsonl"
    prev = (read_jsonl(hist) or [None])[-1] if hist.exists() else None
    changed: list[dict[str, Any]] = []
    if prev and prev["sha256"] != out["sha256"]:
        old = _numeric_leaves(read_json(hdir / f"{pid}.{prev['sha256'][:12]}.json", {}))
        new = _numeric_leaves(read_json(path, {}))
        changed = [{"path": k, "previous": old.get(k), "now": new.get(k)} for k in sorted(set(old) | set(new))
                   if old.get(k) != new.get(k)]
    entry = {"version": (prev or {}).get("version", 0) + 1, "utc": out["utc"], "sha256": out["sha256"],
             "ok": out["ok"], "errors": list(out["errors"]), "copy": str(copy.relative_to(ws.root)),
             "previous_sha256": (prev or {}).get("sha256"), "previous_errors": (prev or {}).get("errors", []),
             "changed_values": changed}
    append_jsonl(hist, entry)
    return entry


def _reference_comparison(ws: Workspace, ins: dict, acc: dict, kb) -> list[str]:
    from . import evidence
    errs: list[str] = []
    ref = ws.meta.get("reference") or {}
    rv = ref.get("values") or {}
    rows = ins.get("reference_comparison") or []
    if not rows:
        return [f"insight.reference_comparison: required for a run that starts from the reference model "
                f"({ref.get('run')} {ref.get('eval')}): one row per deck parameter, the gate work function included"]
    named = {r.get("name") for r in rows}
    missing = [n for n in ws.params_for(acc["deck"]) if n not in named and n not in ws.fixed] if acc.get("deck") else []
    if missing:
        errs.append(f"insight.reference_comparison: missing {missing} (every deck parameter, the aligned gate work function included)")
    for r in rows:
        name = r.get("name")
        where = f"insight.reference_comparison.{name}"
        _need(r, REFCMP_FIELDS, where, errs)
        if r.get("constraint_class") not in CONSTRAINT_CLASSES:
            errs.append(f"{where}: constraint_class must be one of {CONSTRAINT_CLASSES}")
        if name in rv and r.get("reference_value") not in (None, ""):
            try:
                if abs(float(r["reference_value"]) - float(rv[name])) > 1e-6 * max(1.0, abs(float(rv[name]))):
                    errs.append(f"{where}: reference_value {r['reference_value']} differs from the reference card ({rv[name]})")
            except (TypeError, ValueError):
                errs.append(f"{where}: reference_value must be a number for a parameter of the reference card")
        if name in (acc.get("values") or {}) and r.get("final_value") not in (None, ""):
            try:
                v = float(acc["values"][name])
                if abs(float(r["final_value"]) - v) > 1e-6 * max(1.0, abs(v)):
                    errs.append(f"{where}: final_value {r['final_value']} differs from the accepted card ({v})")
            except (TypeError, ValueError):
                errs.append(f"{where}: final_value must be a number")
        errs += evidence.check(ws, r.get("evidence"), f"{where}.evidence", kb=kb)
    _need(ins, ["process_conclusions"], "insight", errs)
    return errs


GATE_FIELDS = ("phi_G_aligned_eV", "phi_G_minus_chi_eV", "dV_trapped_estimate_V", "dV_donor_estimate_V",
               "phi_G_charge_compensated_estimate_eV")
INTERIOR_FIELDS = ("film_vertical", "contacts_lateral", "charge_balance", "contact_voltage_drop")


def _interior_check(ws: Workspace, acc: Mapping[str, Any], ic: Any) -> list[str]:
    """The final card gets one comprehensive interior check: vertical and lateral band cuts at both
    V_D (figures from diagnose --bands of the accepted evaluation), the charge balance and the contact voltage drop."""
    if not isinstance(ic, Mapping):
        return [f"insight.interior_check: required, {{band_diagrams: [figures at V_D {C.VD_KEYS[0]} and {C.VD_KEYS[1]} V], "
                + ", ".join(INTERIOR_FIELDS) + "} for the accepted evaluation"]
    errs = []
    figs = ic.get("band_diagrams") or []
    for vd in C.VD_KEYS:
        want = f"evals/{acc.get('eval')}_bands_vd{vd}.png"
        if want not in figs or not (ws.root / want).exists():
            errs.append(f"insight.interior_check.band_diagrams: {want} (python3 tools/diagnose.py --bands {acc.get('eval')} --vd {vd})")
    for f in INTERIOR_FIELDS:
        if len(str(ic.get(f, "")).strip()) < 30:
            errs.append(f"insight.interior_check.{f}: >= 30 characters, with numbers, labelled as model predictions")
    return errs


def _gate_alignment(ws: Workspace, acc: Mapping[str, Any], ga: Any) -> list[str]:
    """The final insight reports the gate alignment and charge-budget estimates. The numbers are
    those of the accepted evaluation's gate_alignment_estimates. They are approximate estimates, not an extracted work
    function. The interpretation says what they indicate and what these curves cannot separate."""
    if not isinstance(ga, Mapping):
        return ["insight.gate_alignment: required, {" + ", ".join(GATE_FIELDS) + ", interpretation, constraint_class}, "
                "copied from inside_the_device.gate_alignment_estimates of the accepted evaluation"]
    errs = []
    ev = read_json(ws.root / "evals" / f"{acc.get('eval')}.json") or {}
    ref = ((ev.get("bands") or {}).get("gate_alignment_estimates") or {})
    for f in GATE_FIELDS:
        try:
            if abs(float(ga.get(f)) - float(ref.get(f, ga.get(f)))) > 0.005:
                errs.append(f"insight.gate_alignment.{f}: {ga.get(f)} differs from the accepted evaluation ({ref[f]})")
        except (TypeError, ValueError):
            errs.append(f"insight.gate_alignment.{f}: a number")
    if len(str(ga.get("interpretation", "")).strip()) < 80:
        errs.append("insight.gate_alignment.interpretation: >= 80 characters: what the aligned value and the charge-budget "
                    "estimates indicate, compared with the gate metal, and what these curves cannot separate. The "
                    "estimates are approximate and are not an extracted work function")
    if ga.get("constraint_class") not in CONSTRAINT_CLASSES:
        errs.append(f"insight.gate_alignment.constraint_class: one of {CONSTRAINT_CLASSES}")
    return errs


def approval(ws: Workspace, pid: str) -> dict[str, Any]:
    """Return the approval record; raise unless approved for the current proposal text."""
    ap = read_json(ws.root / "approvals" / f"{pid}.json")
    if ap is None:
        raise StateError(f"{pid} has no approval record yet")
    if ap.get("decision") != "approve":
        raise StateError(f"{pid} was not approved: {ap.get('decision')} ({ap.get('comment')})")
    if ap.get("proposal_sha256") != sha256_file(proposal_path(ws, pid)):
        raise StateError(f"{pid} changed after approval; it must be re-approved")
    return ap


def apply_bounds(ws: Workspace, pid: str) -> dict[str, Any]:
    prop = read_json(proposal_path(ws, pid))
    if prop.get("type") != "bounds":
        raise StateError(f"{pid} is not a bounds proposal")
    ap = approval(ws, pid)
    reg = ws.registry()
    for name, spec in prop["bounds"].items():
        reg["params"][name].update({"lo": float(spec["lo"]), "hi": float(spec["hi"]), "basis": spec["basis"],
                                    "bounds_proposal": pid})
    ws.save_registry(reg)
    ws.log("bounds", proposal=pid, bounds=prop["bounds"], approval=ap)
    out = {"applied": pid, "bounds": {n: [reg["params"][n]["lo"], reg["params"][n]["hi"]] for n in prop["bounds"]}}
    moves = {n: float(s["start_resolution"]["new_start"]) for n, s in prop["bounds"].items()
             if isinstance(s.get("start_resolution"), Mapping)
             and not s["lo"] <= ws.accepted()["values"].get(n, reg["params"][n]["init"]) <= s["hi"]}
    if moves:
        acc = ws.accepted()
        changed = {n: [acc["values"].get(n), v] for n, v in moves.items()}
        acc = {"deck": acc["deck"], "values": {**acc["values"], **moves}, "J": None, "eval": None, "utc": now_utc(),
               "calibration_target_met": None, "source": None,
               "note": f"start values moved inside the approved bounds by the explicit resolution in {pid}; the baseline "
                       "is evaluated again"}
        write_json(ws.state / "accepted.json", acc)
        ws.log("start_value_change", proposal=pid, changed=changed,
               reasons={n: prop["bounds"][n]["start_resolution"].get("reason") for n in moves})
        out["start_values_moved"] = changed
        out["note"] = ("The accepted card was moved to the resolved start values and its J cleared: evaluate it again and "
                       "accept it as the baseline.")
    return out


def apply_patch(ws: Workspace, pid: str) -> dict[str, Any]:
    prop = read_json(proposal_path(ws, pid))
    if prop.get("type") != "model_patch":
        raise StateError(f"{pid} is not a model_patch proposal")
    ap = approval(ws, pid)
    reg = ws.registry()
    if any(d.get("proposal") == pid for d in reg["decks"].values()):
        raise StateError(f"{pid} was already applied")
    patch = prop["patch"]
    tpl = D.apply_edits(D.read_templates(ws.deck_dir(patch["base_deck"])), patch["edits"])
    for p in patch.get("new_params", []):
        reg["params"][p["name"]] = {"token": p["token"], "init": float(p["init"]), "lo": float(p["lo"]), "hi": float(p["hi"]),
                                    "scale": p["scale"], "unit": p["unit"], "basis": p["basis"], "meaning": p["meaning"],
                                    "file": "sdevice/par", "since": None, "bounds_proposal": pid}
    tokens = {v["token"].strip("@") for v in reg["params"].values()}
    used = set().union(*(D.tokens_in(D.strip_comments(t)) for t in tpl.values()))
    errs = D.check_template(tpl, tokens & used)
    if errs:
        raise StateError(f"patched deck fails the guard: {errs}")
    new_v = f"v{len(ws.deck_versions()):03d}"
    vdir = ws.deck_dir(new_v)
    vdir.mkdir(parents=True)
    for key, name in D.FILES.items():
        (vdir / name).write_text(tpl[key])
    for p in patch.get("new_params", []):
        reg["params"][p["name"]]["since"] = new_v
    reg["decks"][new_v] = {"parent": patch["base_deck"], "proposal": pid, "utc": now_utc(), "hash": D.deck_hash(vdir),
                           "note": prop.get("title")}
    ws.save_registry(reg)
    ws.log("patch", proposal=pid, deck=new_v, parent=patch["base_deck"], new_params=[p["name"] for p in patch.get("new_params", [])],
           approval=ap)
    return {"deck": new_v, "parent": patch["base_deck"], "new_params": [p["name"] for p in patch.get("new_params", [])]}
