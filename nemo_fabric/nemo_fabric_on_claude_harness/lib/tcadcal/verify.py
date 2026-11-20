"""
Numerical validity and model activation are decided by deterministic checks, never by the agent:

Only VALID evaluations may be accepted or cited as evidence about physics. A failed or mesh-sensitive computation is
never evidence that a model cannot explain the measurement.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from . import config as C
from . import deck as D
from . import objective as O

VALID, SOLVER_FAILED, FALSE_CONVERGENCE = "VALID", "SOLVER_FAILED", "FALSE_CONVERGENCE"
OUT_OF_ENVELOPE, MESH_SENSITIVE = "OUT_OF_ENVELOPE", "MESH_SENSITIVE"
DECK_ERROR = "DECK_ERROR"
ACTIVE, INACTIVE, UNVERIFIED, LOW_SENSITIVITY = "ACTIVE", "INACTIVE", "ACTIVATION_UNVERIFIED", "LOW_SENSITIVITY"


# ---------------------------------------------------------------------------
# Terminal-current balance (false convergence)
# ---------------------------------------------------------------------------
def parse_plt_all(path: Path) -> pd.DataFrame:
    """Every column of a sdevice current file (no grouping)."""
    text = Path(path).read_text(errors="ignore")
    m = re.search(r"datasets\s*=\s*\[(.*?)\]", text, re.S)
    names = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    dm = re.search(r"Data\s*\{(.*?)\n\s*\}", text, re.S)
    if not names or not dm:
        raise ValueError(f"cannot parse {path}")
    toks = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", dm.group(1))
    n = len(names)
    arr = np.array([float(t) for t in toks[: (len(toks) // n) * n]], float).reshape(-1, n)
    return pd.DataFrame(arr, columns=names)


def current_balance(df: pd.DataFrame, floor_a: float = None) -> dict[str, Any]:
    """Largest terminal-current imbalance |I_D + I_S + I_G| / |I_D| over the bias points with |I_D| >= floor, expressed
    in decades of log10 I_D (the unit of the per-point tolerance). Below the floor (the measured detection limit) the
    current does not enter J."""
    floor_a = C.BALANCE_FLOOR_A if floor_a is None else floor_a
    i_d, i_s, i_g = (df[f"{c} TotalCurrent"].to_numpy(float) for c in ("drain", "source", "gate"))
    vg = df["gate OuterVoltage"].to_numpy(float)
    m = np.abs(i_d) >= floor_a
    if not np.any(m):
        return {"max_dec": 0.0, "at_VG": None, "n_points": 0, "ok": True}
    rel = np.abs(i_d + i_s + i_g)[m] / np.abs(i_d[m])
    dec = rel / math.log(10.0)
    k = int(np.argmax(dec))
    return {"max_dec": float(dec[k]), "max_rel": float(rel[k]), "at_VG": float(vg[m][k]), "n_points": int(m.sum()),
            "ok": bool(dec[k] <= C.BALANCE_MAX_DEC)}


# ---------------------------------------------------------------------------
# sdevice log echo
# ---------------------------------------------------------------------------
_NUM = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"


def log_echo(text: str) -> dict[str, Any]:
    """What sdevice reports it applied: electrode boundary conditions, model switches per location, trap entries, and
    parameter-file values that differ from the defaults ("instead of" / "is modified from ... to")."""
    out: dict[str, Any] = {"electrodes": {}, "locations": {}, "parameters": [], "bgn": None, "global": []}
    for m in re.finditer(r'^\s*"(\w+)"\s*:\s*' + _NUM + r'\s*V\s*\((Ohmic|Schottky)(?:,\s*(WorkFunction|Barrier)\s*=\s*('
                         + _NUM + r'))?', text, re.M):
        line = text[m.start():text.find("\n", m.end()) if text.find("\n", m.end()) >= 0 else len(text)]
        rs = re.search(r"\(Resistivity\s*=\s*(" + _NUM + r")\s*Ohms", line)
        out["electrodes"][m.group(1)] = {"type": m.group(2), "key": m.group(3),
                                         "value": float(m.group(4)) if m.group(4) else None,
                                         "resistivity": float(rs.group(1)) if rs else None}
    b = re.search(r"Bandgap narrowing model:\s*(.+)", text)
    out["bgn"] = b.group(1).strip() if b else None
    pm = re.search(r"Physical models:\n(.*?)\n-{10,}", text, re.S)
    if pm:
        out["global"] = [ln.strip() for ln in pm.group(1).splitlines() if ln.strip().startswith(("With ", "Without "))]
    for m in re.finditer(r"The following Physical models (?:have been specified|were changed)\s*\n\s*for (\w+) = '([^']+)'\n"
                         r"(.*?)\n- - - -", text, re.S):
        loc = f"{m.group(1)}={m.group(2)}"
        body = m.group(3)
        traps = []
        for t in re.finditer(r"Trap Entry:.*?(?=Trap Entry:|\Z)", body, re.S):
            tb = t.group(0)
            entry = {"text": " ".join(tb.split())[:300]}
            for key, pat in (("Concentration", r"Concentration\s*=\s*(" + _NUM + ")"),
                             ("Energy", r"Energy\s*=\s*(" + _NUM + ")"), ("EnergySigma", r"Energy Sigma\s*=\s*(" + _NUM + ")"),
                             ("eXsection", r"Electron X-section\s*=\s*(" + _NUM + ")"),
                             ("hXsection", r"Hole X-section\s*=\s*(" + _NUM + ")")):
                mm = re.search(pat, tb)
                if mm:
                    entry[key] = float(mm.group(1))
            traps.append(entry)
        out["locations"].setdefault(loc, {"lines": [], "traps": []})
        out["locations"][loc]["lines"] += [ln.strip() for ln in body.splitlines() if ln.strip()]
        out["locations"][loc]["traps"] += traps
    for m in re.finditer(r"^\s*(\S[^\n:]*?):\s*(\S+)\s*=\s*(" + _NUM + r"),\s*instead of:", text, re.M):
        out["parameters"].append({"model": m.group(1).strip(), "name": m.group(2), "value": float(m.group(3))})
    for m in re.finditer(r'Parameter "([^"]+)" is modified from\s*(' + _NUM + r")\s*to\s*(" + _NUM + ")", text):
        out["parameters"].append({"model": "", "name": m.group(1), "value": float(m.group(3))})
    for m in re.finditer(r"^[ \t]*(\w+):[ \t]*(\w+)[ \t]*=[ \t]*(" + _NUM + r")[ \t]*$", text, re.M):
        out["parameters"].append({"model": m.group(1), "name": m.group(2), "value": float(m.group(3)), "pmi": True})
    out["pmi_ignored"], out["pmi_used"] = {}, []
    for m in re.finditer(r"^[ \t]*([^\n]*?)PMI:[ \t]*\w+/(\w+)[^\n]*$", text, re.M):
        if re.search(r"\(ignored\b", m.group(1)):
            out["pmi_ignored"].setdefault(m.group(2), m.group(0).strip())
        elif m.group(2) not in out["pmi_used"]:
            out["pmi_used"].append(m.group(2))
    return out


def _close(a: float, b: float, rel: float = 2e-4) -> bool:
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-30)


def _token_contexts(templates: Mapping[str, str], token: str) -> list[dict[str, Any]]:
    """Where a parameter token sits in the templates: file, enclosing sdevice section (name, qualifier), electrode entry,
    trap entry keyword."""
    out = []
    tok = f"@{token}@"
    for key in ("sdevice", "par", "sde"):
        text = D.strip_comments(templates[key])
        for m in re.finditer(re.escape(tok), text):
            ctx: dict[str, Any] = {"file": key}
            if key == "sdevice":
                for b in D.top_level_blocks(templates["sdevice"]):
                    if tok in b["body"] or tok in b["qual"]:
                        ctx.update({"section": b["name"], "qual": b["qual"], "body": b["body"]})
                        if b["name"] == "Electrode":
                            for it in D._brace_items(b["body"]):
                                if tok in it:
                                    nm = re.search(r'Name\s*=\s*"([^"]+)"', it)
                                    kw = re.search(r"(\w+)\s*=\s*" + re.escape(tok), it)
                                    ctx.update({"electrode": nm.group(1) if nm else None, "keyword": kw.group(1) if kw else None})
                        else:
                            kw = re.search(r"(\w+)\s*=\s*" + re.escape(tok), b["body"])
                            ctx["keyword"] = kw.group(1) if kw else None
                            ctx["in_traps"] = _inside_traps(b["body"], tok)
                        break
            elif key == "par":
                pre = text[:m.start()]
                blk = re.findall(r'(Material|Region|MaterialInterface|RegionInterface|Electrode)\s*=\s*"([^"]+)"\s*\{', pre)
                ctx["block"] = blk[-1] if blk else None
                kw = re.search(r"(\w+)\s*=\s*$", text[max(0, m.start() - 80):m.start()])
                ctx["keyword"] = kw.group(1) if kw else None
            out.append(ctx)
    return out


def _inside_traps(body: str, tok: str) -> bool:
    """True if the token sits inside a Traps( ... ) statement (nested parentheses allowed)."""
    for m in re.finditer(r"\bTraps\s*\(", body):
        depth, k = 1, m.end()
        while k < len(body) and depth:
            depth += {"(": 1, ")": -1}.get(body[k], 0)
            k += 1
        if tok in body[m.end():k]:
            return True
    return False


TRAP_KEYWORDS = {"conc": "Concentration", "energymid": "Energy", "energysig": "EnergySigma",
                 "exsection": "eXsection", "hxsection": "hXsection"}


def activation(templates: Mapping[str, str], params: Mapping[str, Any], values: Mapping[str, float], echo: Mapping[str, Any],
               readback: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Activation label per deck parameter from the log echo of one run of this deck at these values."""
    out: dict[str, dict[str, Any]] = {}
    for name, spec in params.items():
        token = spec["token"].strip("@")
        v = float(values[name])
        ctxs = _token_contexts(templates, token)
        label, why = UNVERIFIED, "no echo rule for this location"
        for ctx in ctxs:
            if ctx["file"] == "sde" and token == "Nd_eff":
                rb = (readback or {}).get("channel_doping_cm3")
                if rb:
                    label, why = (ACTIVE, "channel donor density read back from the mesh") if _close(rb[0], v, 1e-4) and \
                        _close(rb[1], v, 1e-4) else (INACTIVE, f"mesh carries {rb} cm^-3, requested {v:g}")
                break
            if ctx["file"] == "sdevice" and ctx.get("section") == "Electrode" and ctx.get("electrode"):
                e = echo["electrodes"].get(ctx["electrode"])
                if e is None:
                    label, why = INACTIVE, f"electrode {ctx['electrode']} not in the log echo"
                elif (ctx.get("keyword") or "").lower() == "distresist":
                    # sdevice echoes DistResist as "(Resistivity = <value> Ohms * cm**2)" on the electrode line
                    if e.get("resistivity") is not None and _close(e["resistivity"], v):
                        label, why = ACTIVE, f'electrode {ctx["electrode"]} echo "Resistivity = {e["resistivity"]:g} Ohms*cm^2"'
                    else:
                        label, why = INACTIVE, (f'electrode {ctx["electrode"]} echo shows Resistivity = {e.get("resistivity")}, '
                                                f'the deck requests DistResist = {v:g}')
                elif (ctx.get("keyword") or "").lower() not in ("workfunction", "barrier"):
                    label, why = UNVERIFIED, (f'no echo rule for electrode keyword {ctx.get("keyword")} on {ctx["electrode"]}: '
                                              "verify by read-back (e.g. the contact voltage drop in the current file)")
                elif e["key"] and e["key"].lower() == ctx["keyword"].lower() and _close(e["value"], v):
                    label, why = ACTIVE, f'electrode {ctx["electrode"]} echo "{e["key"]} = {e["value"]:g}"'
                else:
                    label, why = INACTIVE, (f'electrode {ctx["electrode"]} echo shows {e["key"]} = {e["value"]}, the deck '
                                            f'requests {ctx.get("keyword")} = {v:g}')
                break
            if ctx["file"] == "sdevice" and ctx.get("section") == "Physics":
                q = ctx.get("qual", "")
                me = re.search(r'Electrode\s*=\s*"([^"]+)"', q)
                if me and (ctx.get("keyword") or "").lower() == "barrier":
                    e = echo["electrodes"].get(me.group(1))
                    if e and (e["key"] or "").lower() == "barrier" and _close(e["value"], v):
                        label, why = ACTIVE, f'electrode {me.group(1)} echo "Barrier = {e["value"]:g}"'
                    else:
                        label, why = INACTIVE, (f'Barrier {v:g} requested for electrode {me.group(1)} in a Physics section, '
                                                f'but the log echo shows {e and e["key"]} = {e and e["value"]}')
                    break
                if ctx.get("in_traps"):
                    loc_m = re.search(r'(RegionInterface|MaterialInterface|Region|Material)\s*=\s*"([^"]+)"', q)
                    kind = {"RegionInterface": "Interface", "MaterialInterface": "Interface", "Region": "Region",
                            "Material": "Material"}.get(loc_m.group(1)) if loc_m else None
                    field = TRAP_KEYWORDS.get((ctx.get("keyword") or "").lower())
                    cands = [l for k, l in echo["locations"].items()
                             if loc_m and (k == f"{kind}={loc_m.group(2)}" or loc_m.group(2) in k.split("=", 1)[1].split("/"))]
                    if not loc_m:
                        cands = list(echo["locations"].values())
                    hit = any(field and field in t and _close(t[field], v, 2e-3) for l in cands for t in l["traps"])
                    if field is None:
                        label, why = UNVERIFIED, f"trap keyword {ctx.get('keyword')} has no echo rule"
                    elif hit:
                        label, why = ACTIVE, f"trap entry {field} = {v:g} echoed at {loc_m.group(0) if loc_m else 'global'}"
                    else:
                        label, why = INACTIVE, f"no echoed trap entry with {field} = {v:g} at {loc_m.group(0) if loc_m else 'global'}"
                    break
            if ctx["file"] == "par":
                kw = (ctx.get("keyword") or "").lower()
                hits = [p for p in echo["parameters"] if _close(p["value"], v) and (not p.get("pmi") or p["name"].lower() == kw)]
                ign, used = echo.get("pmi_ignored") or {}, set(echo.get("pmi_used") or [])
                off = [p["model"] for p in hits if p.get("pmi") and p["model"] in ign and p["model"] not in used]
                if hits and len(off) == len(hits):
                    label, why = INACTIVE, (f"sdevice read {off[0]}: {hits[0]['name']} = {v:g} from the parameter file, "
                                            f"but its model summary does not apply {off[0]}: \"{ign[off[0]]}\"")
                elif hits:
                    label, why = ACTIVE, "parameter-file value echoed by sdevice"
                else:
                    label, why = UNVERIFIED, (f"parameter-file value {v:g} is not in the log's list of values that differ "
                                              "from the sdevice defaults: it equals the default, or it was not read (an "
                                              "evaluation at another value shows which)")
                break
        out[name] = {"label": label, "why": why}
    return out


def sensitivity_labels(probes: Sequence[Mapping[str, Any]], activation_map: Mapping[str, Mapping[str, Any]] | None = None
                       ) -> dict[str, dict[str, Any]]:
    """LOW_SENSITIVITY from probe runs: every J term moved by less than SENSITIVITY_FRACTION of its tolerance."""
    out = {}
    for p in probes:
        name = p.get("param")
        terms = p.get("change_of_J_terms_in_tolerance_units")
        if name is None or terms is None:
            continue
        mx = max((abs(x) for x in terms.values()), default=0.0)
        act = (activation_map or {}).get(name, {}).get("label")
        low = mx < C.SENSITIVITY_FRACTION
        out[name] = {"max_term_change_tol_units": round(mx, 4),
                     "label": LOW_SENSITIVITY if low and act in (None, ACTIVE) else (act or ACTIVE),
                     "note": ("applied, but the probe step moves every J term by less than "
                              f"{C.SENSITIVITY_FRACTION:g} tolerance at this condition") if low else ""}
    return out


# ---------------------------------------------------------------------------
# TCAD vs TCAD comparison (mesh, solver, bias step): the comparison criteria (config.VERIFY_CRITERIA)
# ---------------------------------------------------------------------------
def _on_grid(vg: np.ndarray, i: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    grid = np.round(np.arange(C.VG_START, C.VG_STOP + 1e-9, C.VG_STEP), 6)
    return grid, O.sim_on_grid(vg, i, grid)


def ss_profile_change(a: Mapping[float, tuple[np.ndarray, np.ndarray]], b: Mapping[float, tuple[np.ndarray, np.ndarray]],
                      preps: Any) -> dict[float, dict[str, Any]]:
    out: dict[float, dict[str, Any]] = {}
    for p in preps:
        vd = float(p.vd)
        if vd not in a or vd not in b:
            continue
        wins = p.extra.get("ss_windows") or O.ss_windows(p.i_abs, p.loq)
        ia, ib = O.sim_on_grid(*a[vd], p.vg), O.sim_on_grid(*b[vd], p.vg)
        sm, sa, sb = O.ss_profile(p.vg, p.i_abs, wins), O.ss_profile(p.vg, ia, wins), O.ss_profile(p.vg, ib, wins)
        rows = []
        for w, m, x, y in zip(wins, sm, sa, sb):
            d = None if None in (m, x, y) or not m else 100.0 * (x - y) / m
            rows.append({"lo_A": w["lo_A"], "hi_A": w["hi_A"], "SS_meas_mV_dec": m, "SS_a_mV_dec": x, "SS_b_mV_dec": y,
                         "change_pct_of_measured": d})
        vals = [abs(r["change_pct_of_measured"]) for r in rows if r["change_pct_of_measured"] is not None]
        out[vd] = {"windows": rows, "max_abs_change_pct_of_measured": max(vals) if vals else None,
                   "reference_criterion_pct": C.VERIFY_CRITERIA["SS_pct"],
                   "note": "reported only; the pass flag uses the fixed single-window SS criterion"}
    return out


def acceptance_set_check(cmp: Mapping[str, Any], a: Mapping[float, tuple[np.ndarray, np.ndarray]],
                         b: Mapping[float, tuple[np.ndarray, np.ndarray]], preps: Any,
                         acceptance: Sequence[str]) -> dict[str, Any]:
    crit = C.VERIFY_CRITERIA
    by_vd = {f"{float(vd):g}": (float(vd), d) for vd, d in cmp["per_vd"].items()}
    preps_by = {f"{float(p.vd):g}": p for p in preps}
    rows: dict[str, dict[str, Any]] = {}
    for t in acceptance:
        name, _, vds = t.partition("@")
        if t == "V_th_shift":
            ch, lim = cmp.get("dVth_shift_mV"), crit["Vth_shift_mV"]
        elif vds not in by_vd:
            rows[t] = {"change": None, "criterion": None, "pass": False, "note": "V_D not compared"}
            continue
        else:
            vd, d = by_vd[vds]
            if name == "V_th":
                ch, lim = d.get("dVth_mV"), crit["Vth_mV"]
            elif name == "I_on":
                ch, lim = d.get("dlogIon_dec"), crit["logIon_dec"]
            elif name == "E_lin":
                ch, lim = d.get("E_lin"), crit["E_lin"]
            elif name == "SS_profile":
                ch, lim = (cmp.get("ss_profile_change") or {}).get(vd, {}).get("max_abs_change_pct_of_measured"), crit["SS_pct"]
            elif name.startswith("region:"):
                p = preps_by[vds]
                ia, ib = O.sim_on_grid(*a[vd], p.vg), O.sim_on_grid(*b[vd], p.vg)
                m = (p.region == name[len("region:"):]) & (ia >= C.BALANCE_FLOOR_A) & (ib >= C.BALANCE_FLOOR_A)
                ch = float(np.max(np.abs(np.log10(ia[m]) - np.log10(ib[m])))) if m.any() else 0.0
                lim = crit["point_dec"]
            else:
                rows[t] = {"change": None, "criterion": None, "pass": False, "note": "unknown term"}
                continue
        ok = ch is not None and abs(ch) <= lim
        rows[t] = {"change": ch, "criterion": lim, "pass": bool(ok)}
    fail = [f"{t}: {r['change']} (criterion {r['criterion']})" for t, r in rows.items() if not r["pass"]]
    return {"terms": rows, "fail": fail, "pass": not fail,
            "basis": "declared acceptance quantities only; "
                     "the all-metric criteria stay reported as `pass`/`fail`"}


def compare_curves(a: Mapping[float, tuple[np.ndarray, np.ndarray]], b: Mapping[float, tuple[np.ndarray, np.ndarray]],
                   ss_meas_mV: Mapping[float, float], preps: Any = None,
                   acceptance: Sequence[str] | None = None) -> dict[str, Any]:
    """Per-metric difference of curve set ``a`` against reference ``b`` (same card, same gate work function, no
    alignment), keyed by V_D. Metrics are extracted by objective.metric_branch with ``b`` in the place of the measurement,
    on the measured V_G grid. Returns the deltas, the criteria (fractions of the J tolerances) and pass flags."""
    out: dict[str, Any] = {"per_vd": {}, "fail": []}
    vth = {}
    for vd in sorted(set(a) & set(b)):
        gb, ib = _on_grid(*b[vd])
        _, ia = _on_grid(*a[vd])   # both curves through the same interpolation onto the measured V_G grid
        br = O.metric_branch(gb, ia, gb, ib, C.ss_low("main"))
        above = (np.abs(ia) >= C.BALANCE_FLOOR_A) & (np.abs(ib) >= C.BALANCE_FLOOR_A)
        dpt = float(np.max(np.abs(np.log10(np.abs(ia[above])) - np.log10(np.abs(ib[above]))))) if above.any() else 0.0
        d = {"dVth_mV": None if br["dVth_V"] is None else 1e3 * br["dVth_V"],
             "dSS_pct_of_measured": None if br["dSS_V_dec"] is None else 100 * 1e3 * br["dSS_V_dec"] / ss_meas_mV[vd],
             "dlogIon_dec": br["dlogIon_dec"], "E_lin": br["E_lin"], "max_point_dec": dpt,
             "Vth_a_V": br["Vth_sim_V"], "Vth_b_V": br["Vth_meas_V"]}
        out["per_vd"][vd] = d
        vth[vd] = (br["Vth_sim_V"], br["Vth_meas_V"])
        crit = C.VERIFY_CRITERIA
        checks = (("dVth_mV", crit["Vth_mV"]), ("dSS_pct_of_measured", crit["SS_pct"]), ("dlogIon_dec", crit["logIon_dec"]),
                  ("E_lin", crit["E_lin"]), ("max_point_dec", crit["point_dec"]))
        undefined_both = {"dVth_mV": br["Vth_sim_V"] is None and br["Vth_meas_V"] is None,
                          "dSS_pct_of_measured": br["SS_sim_mV_dec"] is None and br["SS_meas_mV_dec"] is None}
        d["undefined_in_both"] = [k for k, v in undefined_both.items() if v]
        for k, lim in checks:
            if d[k] is None and undefined_both.get(k):
                continue
            if d[k] is None or abs(d[k]) > lim:
                out["fail"].append(f"V_D {vd:g} V: {k} = {d[k]} (criterion {lim})")
    vds = sorted(vth)
    if len(vds) >= 2 and all(None not in vth[v] for v in vds[:2]):
        lo, hi = vds[0], vds[-1]
        sh = 1e3 * ((vth[hi][0] - vth[lo][0]) - (vth[hi][1] - vth[lo][1]))
        out["dVth_shift_mV"] = sh
        if abs(sh) > C.VERIFY_CRITERIA["Vth_shift_mV"]:
            out["fail"].append(f"V_th shift difference {sh:.3f} mV (criterion {C.VERIFY_CRITERIA['Vth_shift_mV']})")
    out["pass"] = not out["fail"]
    if preps is not None:
        out["ss_profile_change"] = ss_profile_change(a, b, preps)
        if acceptance:
            out["acceptance_set"] = acceptance_set_check(out, a, b, preps, acceptance)
    out["change_x3_indicator"] = {vd: {k: (None if v is None else 3 * abs(v)) for k, v in d.items()
                         if not k.startswith("Vth_") and k != "undefined_in_both"}
                    for vd, d in out["per_vd"].items()}
    return out


# ---------------------------------------------------------------------------
# Envelope: numerically verified parameter ranges per deck version
# ---------------------------------------------------------------------------
def envelope_margin(spec: Mapping[str, Any]) -> tuple[str, float]:
    """Distance from a verified card within which a parameter counts as verified."""
    if spec.get("scale") == "log":
        return "log10", C.ENVELOPE_LOG_DEC
    unit = (spec.get("unit") or "").strip()
    if unit == "eV":
        return "abs", C.ENVELOPE_EV
    return "rel", C.ENVELOPE_REL


def within_envelope(values: Mapping[str, float], params: Mapping[str, Any], verified_cards: Sequence[Mapping[str, float]]
                    ) -> tuple[bool, list[str]]:
    """True if some verified card lies within the margin of every parameter (WF_gate excluded: a rigid V_G shift)."""
    if not verified_cards:
        return False, ["no verified card for this deck version"]
    best_miss: list[str] | None = None
    for card in verified_cards:
        miss = []
        for name, spec in params.items():
            if name == "WF_gate" or name not in card or name not in values:
                continue
            kind, mg = envelope_margin(spec)
            a, b = float(values[name]), float(card[name])
            if kind == "log10":
                d = abs(math.log10(max(a, 1e-300)) - math.log10(max(b, 1e-300)))
            elif kind == "abs":
                d = abs(a - b)
            else:
                d = abs(a - b) / max(abs(b), 1e-30)
            if d > mg:
                miss.append(f"{name} = {a:g}: outside the numerically verified range")
        if not miss:
            return True, []
        if best_miss is None or len(miss) < len(best_miss):
            best_miss = miss
    return False, best_miss or []


def silicon_template_models(templates: Mapping[str, str]) -> list[dict[str, str]]:
    sdev = D.strip_comments(templates["sdevice"])
    par_iwo = D.par_material_blocks(templates["par"]).get(C.CHANNEL, "")
    out = []
    for pat, section in C.SILICON_TEMPLATE_SECTIONS.items():
        if re.search(pat, sdev) and not re.search(rf"\b{section}\b", par_iwo):
            out.append({"model": section, "note": f"switched on in the deck; {C.CHANNEL} uses the "
                                                   f"{C.TEXT['PAR_TEMPLATE']}-template '{section}' values of {C.CHANNEL_PAR} "
                                                   f"(no {C.CHANNEL}-specific source)"})
    return out
