#!/usr/bin/env python3
"""Build a fresh, isolated agent workspace: runs/<run_id>/workspace.

The workspace holds only: the instructions (AGENTS.md, shared CLAUDE.md and skills), the calibration anchors
(forward branch at the two calibration drain biases), the device facts, the start deck,
the manual KB, the tools, and empty state. No held-out data, no earlier results.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from tcadcal import config as C  # noqa: E402
from tcadcal import deck as D  # noqa: E402
from tcadcal import measurements as M  # noqa: E402
from tcadcal import sentaurus as S  # noqa: E402
from tcadcal.state import init_workspace_state  # noqa: E402
from tcadcal.util import now_utc, read_json, sha256_file, write_json  # noqa: E402

TOOL_WRAPPER = '''#!/usr/bin/env python3
"""Agent tool `{name}` (see .claude/skills/calibration-loop). Implementation: tcadcal.cli."""
import sys
sys.path.insert(0, "{lib}")
from tcadcal.cli import TOOLS
sys.exit(TOOLS["{name}"](sys.argv[1:]))
'''


AGENT_BACKENDS = ("fabric",)
FABRIC_TEXT = {
    ".claude/skills/literature-evidence/SKILL.md": [
        ("- Use WebSearch for peer-reviewed papers", "- Use the web search tool (`mcp__exa__web_search_exa`) for peer-reviewed papers"),
        ("- Use WebFetch to read the abstract", "- Use the page fetch tool (`mcp__exa__web_fetch_exa`) to read the abstract"),
    ],
}


def apply_fabric_text(ws: Path) -> None:
    for rel, pairs in FABRIC_TEXT.items():
        f = ws / rel
        t = f.read_text()
        for old, new in pairs:
            if t.count(old) != 1:
                raise SystemExit(f"fabric text: {rel}: expected one '{old[:40]}...'")
            t = t.replace(old, new)
        f.write_text(t)




def git_head() -> str:
    try:
        return subprocess.run(["git", "-C", str(HERE), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


RULES_DEFAULT = {"main": [], "recal": ["physics_order", "step_evidence", "reference_comparison", "align_start_last"]}
RULES_DEFAULT_J = {"main": ["physics_order", "step_evidence", "align_start_last", "fixed_mesh", "internal_reading", "band_reading"],
                   "recal": RULES_DEFAULT["recal"]}

OBJECTIVE_TEXT = {"J_objective": {"main": "objective.md", "recal": "objective.md"}}
FULL_TEXT = {
    "CLAUDE.md": [
        ('The gate work function is set automatically after every\n   simulation by aligning the curves along V_G. It only shifts the transfer curve and is not one\n   of your search parameters, but it is a calibrated quantity: a bound it reaches is explained like\n   any other bound.',
         "The gate offset (WF_gate) is set automatically after every\n   simulation: one common offset for both drain biases, "
         "at the minimum of J\n   (`inputs/objective.md`). It is not one of your search parameters and has no bounds."),
        ('Propose bounds for the deck parameters with a physical or\n   literature basis for each range.',
         "Propose bounds for the deck parameters (not WF_gate, which is\n   set by the gate alignment and has no bounds) with a "
         "physical or literature basis for each range."),
    ],
    ".claude/skills/calibration-loop/SKILL.md": [
        ('The gate work function (WF_gate) is aligned after every simulation and is not one of your search\nparameters. It is still a calibrated quantity: when it stops at an approved bound, explain it like\nany other bound.',
         "The gate offset (WF_gate) is set after every simulation by the gate alignment, one common offset\n"
         "for both drain biases at the minimum of J (`inputs/objective.md`). It is not one of your search\n"
         "parameters. It has no bounds, and you do not propose any."),
        ('for every deck parameter.\n   For WF_gate the bounds limit the work-function alignment.',
         "for every deck parameter\n   except WF_gate (set by the gate alignment, no bounds)."),
        ("<only when a parameter or the gate alignment sits at a bound:", "<only when a parameter sits at a bound:"),
        ("When a parameter or the gate alignment stops at an approved bound,", "When a parameter stops at an approved bound,"),
        ("The gate alignment (WF_gate) takes no direction, only a basis.", "WF_gate is not proposed (it is set by the gate alignment)."),
    ],
    ".claude/skills/residual-diagnostics/SKILL.md": [
        ("Flat-band voltage. Handled automatically by the gate work-function alignment while the alignment is inside its "
         "interval. When it stops at an interval bound, the common offset stays in the V_th terms",
         "Flat-band voltage. Set by the gate alignment after every simulation: one common offset for both drain biases, at "
         "the minimum of J. A V_th error that remains at either drain bias is the part that one common offset cannot "
         "remove"),
    ],
}


def apply_full_objective_text(ws: Path) -> None:
    """Objective J_objective with the full-objective gate alignment: the alignment section of inputs/objective.md and the
    alignment passages of CLAUDE.md and the skills (FULL_TEXT). Everything else stays the J_objective text."""
    f = ws / "inputs" / "objective.md"
    t = f.read_text()
    m = re.search(r"(?ms)^## Gate work-function alignment\n.*?(?=^## )", t)
    if not m:
        raise SystemExit("full_objective: alignment section not found in inputs/objective.md")
    f.write_text(t.replace(m.group(0), (HERE / "templates" / "objective_full.md").read_text()))
    for rel, pairs in FULL_TEXT.items():
        g = ws / rel
        u = g.read_text()
        for old, new in pairs:
            if u.count(old) != 1:
                raise SystemExit(f"full_objective: passage not found once in {rel}: {old[:60]!r}")
            u = u.replace(old, new)
        g.write_text(u)


def objective_text(objective: str, program: str) -> str:
    if program not in OBJECTIVE_TEXT[objective]:
        raise SystemExit(f"objective {objective} has no text for program {program}")
    t = (HERE / "templates" / OBJECTIVE_TEXT[objective][program]).read_text()
    if objective == "J_objective" and program == "recal":
        dev = C.calibration(program)[0][0]
        nvg, v0 = C.vtext(C.noise_vg_max(dev)), C.vtext(C.vg_start(dev))
        for old, new in [("each of its [[N_FORWARD]] forward points", f"each of its {C.n_forward(dev)} forward points"),
                         ("at V_G <= [[NOISE_VG_MAX]] V.", f"at V_G <= {nvg} V\n  (the sweep starts at {v0} V, and every reading up "
                                                           f"to {nvg} V is instrument noise).")]:
            if t.count(old) != 1:
                raise SystemExit(f"objective text: passage not found once: {old!r}")
            t = t.replace(old, new)
    return t

PHYSICS_ORDER_SECTION = """
## Priority rule: residual, physical reading, targeted search, then action
This rule has the highest priority and overrides every other instruction. Before every bounds proposal,
parameter update and physics-model addition, work in this order:
1. **Residual.** Read the current evaluation: which metrics or regions are outside tolerance, at which
   V_D, with numbers.
2. **Physical reading.** Name the device quantity that governs each residual and, when you start from a
   reference model, the direction in which this device should differ from the reference.
3. **Targeted search.** Search the literature and the Sentaurus manual for that reading, after you have
   read the residual. Record what you use (`kb/literature/<key>.md`, manual chunks with quotes).
4. **Action that follows the reading.** Bounds, parameter updates and model additions must follow the
   direction you stated. If your reading expects a parameter below (above) its reference value, its upper
   (lower) bound is the reference value, unless a cited source supports a change the other way.

The tools check this order (run rule `physics_order`, skill `calibration-loop`).
"""

MODEL_ADDITION_SECTION = """
## Model additions
Every model family added by a patch needs an activation check. The model-patch proposal states, in the field
`activation_check`, the sdevice log echo or the field read-back that will show each added model is applied at the
intended location and value. The smoke run must show it. Its evaluations carry the flag for values outside the
numerically verified range, and the final model is checked on a refined mesh after the run. The start deck itself
(including its source and drain contacts) stays available as it is.
"""

REFERENCE_SECTION = """
## This run: recalibration from a reference model
- The device in `inputs/DEVICE_INFO.md` has the same structure as a reference device but a different
  [[RECAL_DIFFERENCE]].
- `state/decks/v000/` is the reference model: the deck and the card calibrated on the reference
  device by an earlier run. Its parameter values are the start values. Its bounds are cleared, so
  propose bounds for this device at G1, with a basis.
- `inputs/reference/`: the reference card, its final insight, its step log and the measured
  reference curves (forward, V_D = [[VD_LO]] and [[VD_HI]] V). They are context for comparing the two
  devices, not calibration data.
- Your physical readings should say what differs between the two devices and which descriptor of
  the model accounts for each difference.
- The descriptors are calibration-effective, not microscopic extractions. The gate work function is
  aligned as before. Report its change next to the channel descriptors, as an effective flat-band
  term.

## Evidence at every step (run rules, see `state show`)
- `step_evidence`: every accepted step after the baseline carries an `evidence` block in its step
  record (skill `calibration-loop`): a Sentaurus manual chunk or a literature note, each with a
  verbatim quote, supporting the physical reading or the plausibility statement.
- `reference_comparison`: the final insight compares every deck parameter with the reference card
  (skill `calibration-loop`).
"""


FIXED_SECTION = """
## Fixed work functions (run rule `fixed_work_functions`)
- The gate, source and drain electrodes are the same [[GATE_METAL]] metal, on the same gate stack, as in the
  reference device. Their effective work functions are therefore fixed inputs at the reference
  values: {values}.
- They are not aligned, bounded, probed or fitted. The tools reject any change to them.
- Every difference between the two devices must be accounted for by the other deck parameters or
  by a physics-model addition.
"""

RECAL_SECTION = """
## Gate offset and shared contacts (run rules `fixed_work_functions`, `parameters_only`)
- This run is a [[RECAL_KIND]] recalibration of the reference model on the [[RECAL_DEVICE]].
- The gate offset is set by the same gate alignment as in the reference calibration, one common
  offset for both drain biases at the minimum of J (`inputs/objective.md`). It starts at the
  reference model's value and is recomputed for every candidate model of this device.
- The source/drain contact parameters are held at the reference values: {values}. This is a
  shared-contact assumption of this run, not an established composition-invariant property. They
  are not bounded, probed or fitted, and the tools reject any change to them.
- Only the declared channel parameters are recalibrated (`inputs/objective.md`). Model changes are
  not part of this run. A residual they cannot remove within justified bounds is reported as a
  finding.
"""

FIXED_TEXT = {
    "CLAUDE.md": [
        ("The gate work function is set automatically after every\n   simulation by aligning the curves along V_G. "
         "It only shifts the transfer curve and is not one\n   of your search parameters, but it is a calibrated quantity: "
         "a bound it reaches is explained like\n   any other bound.",
         "The gate and source/drain work functions are fixed in\n   this run (section \"Fixed work functions\"). "
         "No alignment is made."),
        ("The gate work function is\n  aligned as before. Report its change next to the channel descriptors, as an "
         "effective flat-band\n  term.",
         "The gate and source/drain\n  work functions are fixed at the reference values (section \"Fixed work functions\")."),
    ],
    "inputs/DEVICE_INFO.md": [
        ("| Source/drain contacts | Pd, 40 nm, on top of the channel |",
         "| Gate electrode | Pd (the same metal as the source/drain contacts) |\n"
         "| Source/drain contacts | Pd, 40 nm, on top of the channel |"),
    ],
    ".claude/skills/calibration-loop/SKILL.md": [
        ("Sentaurus at both calibration biases, gate work-function alignment, J and its terms,",
         "Sentaurus at both calibration biases, J and its terms,"),
        ("The gate work function (WF_gate) is aligned after every simulation and is not one of your search\n"
         "parameters. It is still a calibrated quantity: when it stops at an approved bound, explain it like\n"
         "any other bound.",
         "The fixed work functions (WF_gate, WF_sd, run rule `fixed_work_functions`) are not aligned,\n"
         "bounded or fitted."),
        ("for every deck parameter.\n   For WF_gate the bounds limit the work-function alignment.",
         "for every deck parameter\n   except the fixed work functions."),
        ("one entry per deck parameter except WF_gate,", "one entry per deck parameter except the fixed work functions,"),
        ("one row per deck parameter, the aligned gate work function included,",
         "one row per deck parameter except the fixed work functions,"),
    ],
    ".claude/skills/residual-diagnostics/SKILL.md": [
        ("Flat-band voltage. Handled automatically by the gate work-function alignment while the alignment is inside its "
         "interval. When it stops at an interval bound, the common offset stays in the V_th terms",
         "Flat-band voltage: the work-function difference (fixed in this run) and the charge in the channel "
         "and at its interfaces"),
    ],
    ".claude/skills/deck-patching/SKILL.md": [
        ("- **The gate electrode entry.** Its work function is set by the alignment. The source and drain\n"
         "  entries are not fixed beyond their start bias: everything else in them may change.",
         "- **The work functions.** The gate electrode entry and the source/drain `Workfunction=@WF_sd@` "
         "tokens (fixed inputs in this run)."),
    ],
}


OBJECTIVE_TEXT_EDITS = {
    "CLAUDE.md": [("SS within 15 %, log10 I_on", "the SS profile within 15 % in every current window, log10 I_on"),
                  ("V_th at 100 nA within 50 mV", "V_th at [[VTH_LEVEL]] ([[VTH_PER_SQUARE]] x W/L) within 50 mV"),
                  ("- the drain-induced V_th shift, V_th([[VD_HI]] V) - V_th([[VD_LO]] V), within 15 % of the measured shift\n",
                   "- (the drain-bias threshold shift, V_th([[VD_HI]] V) - V_th([[VD_LO]] V), and g_m,max are reported in every\n"
                   "  evaluation, but they are not terms of J or of the target)\n")],
    ".claude/skills/calibration-loop/SKILL.md": [('"targeted": ["SS@[[VD_LO_G]]", "SS@[[VD_HI_G]]"],', '"targeted": ["SS_profile@[[VD_LO_G]]", "SS_profile@[[VD_HI_G]]"],'),
                                                 ("`V_th@<VD>`, `SS@<VD>`,", "`V_th@<VD>`, `SS_profile@<VD>`,"),
                                                 ("  `V_th_shift` or `region:", "  `V_th_shift` (reported, not a J term) or `region:")],
    ".claude/skills/residual-diagnostics/SKILL.md": [("e.g. `SS@[[VD_LO_G]]`, `SS@[[VD_HI_G]]`,", "e.g. `SS_profile@[[VD_LO_G]]`, `SS_profile@[[VD_HI_G]]`,")],
}


def check_frozen_ss_windows(program: str) -> str:
    import hashlib
    from tcadcal import measurements as M
    from tcadcal import objective as O
    p = C.SS_WINDOWS_FROZEN_BY_PROGRAM.get(program)
    if p is None or not p.exists():
        return None   # no frozen windows: J computes the SS windows from each measured curve
    for dev, vd in C.calibration(program):
        src = Path(M.load_forward(dev, vd).source)
        sha = hashlib.sha256(src.read_bytes()).hexdigest()
        if sha != O.frozen_ss_source(dev, vd):
            raise SystemExit(f"objective text: {src.name} differs from the file the SS windows were frozen from")
    return hashlib.sha256(p.read_bytes()).hexdigest()


def apply_objective_text(ws: Path, skip: tuple[str, ...] = ()) -> None:
    for rel, pairs in OBJECTIVE_TEXT_EDITS.items():
        if rel in skip:
            continue
        f = ws / rel
        t = f.read_text()
        for old, new in pairs:
            if t.count(old) != 1:
                raise SystemExit(f"objective text: passage not found once in {rel}: {old[:60]!r}")
            t = t.replace(old, new)
        f.write_text(t)


RECAL_FULL_TEXT = {
    "CLAUDE.md": [
        ('- `inputs/reference/`: the reference card, its final insight, its step log and the measured\n  reference curves', '- `inputs/reference/`: the reference card and the measured\n  reference curves'),
        ("The gate work function is\n  aligned as before. Report its change next to the channel descriptors, as an "
         "effective flat-band\n  term.",
         "The gate offset is set by\n  the gate alignment (one common offset for both drain biases, at the minimum of J). "
         "Report its\n  change next to the channel descriptors, as an effective electrostatic offset, not as a measured\n"
         "  work-function change."),
    ],
    ".claude/skills/calibration-loop/SKILL.md": [
        ("for every deck parameter\n   except WF_gate (set by the gate alignment, no bounds).",
         "for every declared channel parameter\n   (`inputs/objective.md`). WF_gate is set by the gate alignment and has no "
         "bounds."),
    ],
    ".claude/skills/deck-patching/SKILL.md": [('- **The gate electrode entry.** Its work function is set by the alignment. The source and drain\n  entries are not fixed beyond their start bias: everything else in them may change.', '- **The electrode entries.** The gate work function is set by the alignment. The source and drain\n  entries are fixed in this run (shared contacts).')],
}


def recal_full_section(fixed_vals: Mapping[str, float], declared: list[str], wf0: float) -> str:
    fx = ", ".join(f"{n} = {v:g}" for n, v in fixed_vals.items())
    return f"""## This run: recalibration on the [[RECAL_DEVICE]]
- **Gate offset.** One [[RECAL_KIND]] effective gate offset, set by the gate alignment above (one common offset
  for both drain biases, at the minimum of J). The first alignment starts at the reference model's value,
  WF_gate = {wf0:g} eV, and the offset is computed for every candidate model of this device. It is not a measured [[GATE_METAL]]
  work function.
- **Shared contacts.** The source/drain contact parameters are held at the reference values: {fx}. This is a
  shared-contact assumption of this run, not an established composition-invariant property.
- **What you recalibrate.** The declared channel parameters only: {", ".join(declared)}. Model changes are not part of
  this run (run rule `parameters_only`).
- **Bounds.** Each bound needs its physical or literature basis. A residual that the declared parameters cannot remove
  within justified bounds is a finding to report, not a reason to widen a bound.

"""


def apply_recal_full_text(ws: Path, program: str, fixed_vals: Mapping[str, float], declared: list[str],
                          wf0: float) -> None:
    """Recalibration texts under the full-objective gate alignment (apply_full_objective_text already applied)."""
    f = ws / "inputs" / "objective.md"
    t = f.read_text()
    m = re.search(r"(?ms)^## Gate offset: one common offset set by the full objective\n.*?(?=^## )", t)
    if not m:
        raise SystemExit("recalibration: gate-offset section not found in inputs/objective.md")
    sec = m.group(0)
    vg0 = C.vg_start(C.calibration(program)[0][0])
    old_pts = "J uses the measured [[VG_START]] to [[VG_STOP]] V points only."
    if sec.count(old_pts) != 1:
        raise SystemExit("recalibration: measured-range sentence not found once in the gate-offset section")
    sec2 = sec.replace(old_pts, f"J uses the measured {vg0:g} to [[VG_STOP]] V points only.")
    f.write_text(t.replace(sec, sec2 + recal_full_section(fixed_vals, declared, wf0)))
    for rel, pairs in RECAL_FULL_TEXT.items():
        g = ws / rel
        u = g.read_text()
        for old, new in pairs:
            if u.count(old) != 1:
                raise SystemExit(f"recalibration: passage not found once in {rel}: {old[:60]!r}")
            u = u.replace(old, new)
        g.write_text(u)


def apply_fixed_text(ws: Path, program: str) -> None:
    # the alignment section of the objective text, whatever the objective and program
    f = ws / "inputs" / "objective.md"
    t = f.read_text()
    m = re.search(r"(?ms)^## Gate work-function alignment\n.*?(?=^## )", t)
    if not m:
        raise SystemExit("fixed_work_functions: alignment section not found in inputs/objective.md")
    vg0 = C.vg_start(C.calibration(program)[0][0])
    f.write_text(t.replace(m.group(0), f"""## Gate work function (fixed in this run)
The gate and source/drain work functions are fixed at the reference values (run rule
`fixed_work_functions`). No alignment is made: J and the device metrics are computed on the
simulated curves as they are, and every evaluation reports `alignment defined: false`.
- The simulated sweep covers [[VG_SIM_START]] to [[VG_SIM_STOP]] V. J uses the measured {vg0:.1f} to [[VG_STOP]] V points only.

"""))
    for rel, pairs in FIXED_TEXT.items():
        f = ws / rel
        t = f.read_text()
        for old, new in pairs:
            if rel == "inputs/DEVICE_INFO.md" and t.count(old) == 0:
                continue   # a device file of another device: the contact line differs
            if t.count(old) != 1:
                raise SystemExit(f"fixed_work_functions: passage not found once in {rel}: {old[:60]!r}")
            t = t.replace(old, new)
        f.write_text(t)


def reference_from(run_id: str, card: str = "frozen") -> dict:
    src = C.RUNS_ROOT / run_id / "workspace"
    if card == "selected":
        sel = read_json(C.RUNS_ROOT / run_id / "final" / "selection.json")
        if not sel or not sel.get("selected"):
            raise SystemExit(f"{run_id}: no selected model (run postrun.py select first)")
        ev = read_json(src / "evals" / f"{sel['selected']}.json")
        reg = read_json(src / "state" / "registry.json")["params"]
        deck_dir = src / "state" / "decks" / ev["deck"]
        tokens = set().union(*(D.tokens_in(D.strip_comments(t)) for t in D.read_templates(deck_dir).values()))
        return {"run": run_id, "eval": ev["eval_id"], "deck": ev["deck"], "J": ev["J"], "values": dict(ev["values"]),
                "deck_dir": deck_dir, "params": {k: v for k, v in reg.items() if v["token"].strip("@") in tokens},
                "insight": None, "summary": None, "step_log": None, "card": "selected",
                "program": read_json(src / ".tcadcal_workspace.json").get("program", "main")}
    frozen = read_json(src / "state" / "FROZEN")
    if not frozen:
        raise SystemExit(f"{run_id} is not frozen")
    acc = frozen["accepted"]
    reg = read_json(src / "state" / "registry.json")["params"]
    deck_dir = src / "state" / "decks" / acc["deck"]
    tokens = set().union(*(D.tokens_in(D.strip_comments(t)) for t in D.read_templates(deck_dir).values()))
    params = {k: v for k, v in reg.items() if v["token"].strip("@") in tokens}
    final = read_json(src / "proposals" / f"{frozen['proposal']}.json") or {}
    return {"run": run_id, "eval": acc["eval"], "deck": acc["deck"], "J": acc["J"], "values": acc["values"],
            "deck_dir": deck_dir, "params": params, "insight": final.get("insight"), "summary": final.get("summary"),
            "step_log": src / "state" / "STEP_LOG.md", "program": read_json(src / ".tcadcal_workspace.json").get("program", "main")}


def build(run_id: str, cap: int, role: str, program: str = "main", start_from: str | None = None,
          rules: list[str] | None = None, fixed: list[str] | None = None, objective: str = "J_objective",
          alignment: str | None = None, recal: bool = False, agent_backend: str = "fabric",
          agent_model: str | None = None, text_only_model: bool = False) -> Path:
    if agent_backend not in AGENT_BACKENDS:
        raise SystemExit(f"unknown agent backend {agent_backend}; use one of {AGENT_BACKENDS}")
    if objective not in C.OBJECTIVES:
        raise SystemExit(f"unknown objective {objective}; use one of {C.OBJECTIVES}")
    if alignment not in C.ALIGNMENT_LOSSES:
        raise SystemExit(f"unknown alignment {alignment}; use one of {C.ALIGNMENT_LOSSES}")
    if alignment == "full_objective" and (objective != "J_objective" or ((start_from or fixed) and not recal)):
        raise SystemExit("the full-objective gate alignment is defined for J_objective from the start deck, or for --recalibrate")
    if recal and (program != "recal" or objective != "J_objective" or not start_from
                  or alignment != "full_objective" or not fixed or "WF_gate" in fixed):
        raise SystemExit("--recalibrate needs --objective J_objective, --start-from, --alignment full_objective and "
                         "--fixed with the contact parameters (not WF_gate)")
    if program == "recal" and not recal:
        raise SystemExit("a recalibration needs --recalibrate and --start-from")
    root = C.RUNS_ROOT / run_id
    ws = root / "workspace"
    if ws.exists():
        raise SystemExit(f"{ws} exists; a workspace is built once")
    required = [M.meas_path(dev, vd) for dev, vd in C.calibration(program)]
    required += [HERE / "kb_cache" / "manual" / name for name in ("chunks.jsonl", "meta.json")]
    required += [C.SYNOPSYS_SUPPORT_DIR / name for name in C.SYNOPSYS_SUPPORT_FILES]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("Workspace inputs missing; see README.md before building:\n" + "\n".join(missing))
    for d in ("inputs/meas", "kb/literature", "proposals", "approvals", "notes", "logs", "evals", "jobs", "tools",
              ".claude/skills", "runs"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    (root / "orchestrator").mkdir(parents=True, exist_ok=True)
    (C.ROOT / "tmp").mkdir(exist_ok=True)
    ref = reference_from(start_from, "selected" if recal else "frozen") if start_from else None
    if recal and rules is None:
        rules = ["physics_order", "step_evidence", "reference_comparison", "align_start_last", "fixed_mesh",
                 "internal_reading", "band_reading", "parameters_only"]
    rules = list((RULES_DEFAULT_J if objective in C.TERM_OBJECTIVES else RULES_DEFAULT)[program] if rules is None else rules)
    fixed_vals = {}
    if fixed:
        if not ref:
            raise SystemExit("--fixed needs --start-from (the values come from the reference card)")
        fixed_vals = {n: ref["values"][n] for n in fixed}
        # fixed parameters: the start-from-last-alignment rule does not apply outside a recalibration
        rules = ([r for r in rules if r != "align_start_last"] if not recal else list(rules)) + ["fixed_work_functions"]
    # instructions
    text = (HERE / "templates" / "CLAUDE.md.tmpl").read_text().replace("{RUN_ID}", run_id)
    if "physics_order" in rules:
        text = text.replace("\n## Inputs (read these first)", PHYSICS_ORDER_SECTION + "\n## Inputs (read these first)", 1)
    fx_text = (RECAL_SECTION.format(values=", ".join(f"{n} = {v:g}" for n, v in fixed_vals.items())) if recal else
               FIXED_SECTION.format(values=", ".join(f"{n} = {v:g} eV" for n, v in fixed_vals.items()))) if fixed_vals else ""
    ml_text = MODEL_ADDITION_SECTION if "fixed_mesh" in rules else ""
    (ws / "CLAUDE.md").write_text(text + (REFERENCE_SECTION if ref else "") + fx_text + ml_text)
    for sk in sorted((HERE / "skills").iterdir()):
        shutil.copytree(sk, ws / ".claude" / "skills" / sk.name)
    # inputs: calibration anchors only (forward branch)
    for dev, vd in C.calibration(program):
        fw = M.load_forward(dev, vd)
        fw.frame().to_csv(ws / "inputs" / "meas" / f"{dev}_VD{M.vd_tag(vd)}V_forward.csv", index=False)
    info = "DEVICE_INFO_recal.md" if recal else "DEVICE_INFO.md"
    shutil.copy2(C.DEVICE_DIR / info, ws / "inputs" / "DEVICE_INFO.md")
    if ref:
        rd = ws / "inputs" / "reference"
        (rd / "meas").mkdir(parents=True)
        for dev, vd in C.calibration(ref["program"]):
            M.load_forward(dev, vd).frame().to_csv(rd / "meas" / f"{dev}_VD{M.vd_tag(vd)}V_forward.csv", index=False)
        write_json(rd / "card.json", {"run": ref["run"], "eval": ref["eval"], "deck": ref["deck"], "J": ref["J"],
                                      "values": ref["values"], "summary": ref["summary"]})
        if ref.get("card") == "selected":
            # recalibration: the selected model and its deck only, no narrative of the reference run
            (rd / "README.md").write_text(
                "# Reference model\n\nThe model calibrated on the reference device ([[REFERENCE_DEVICE]]): deck "
                f"{ref['deck']} (= this run's v000) and the parameter values of its selected card (J = {ref['J']:.3f} on "
                "the reference data).\n\n"
                "- `card.json`: parameter values, with the gate offset as set on the reference data.\n"
                "- `meas/`: the reference device's calibration curves (forward branch). Context only, not calibration data.\n")
        else:
            write_json(rd / "insight.json", ref["insight"] or {})
            shutil.copy2(ref["step_log"], rd / "STEP_LOG.md")
            (rd / "README.md").write_text(
                f"# Reference model\n\nFinal model of run {ref['run']} on the reference device ([[REFERENCE_DEVICE]]): "
                f"deck {ref['deck']} (= this run's v000), card {ref['eval']} (J = {ref['J']:.3f} on the reference data).\n\n"
                "- `card.json`: parameter values, with the gate work function as aligned on the reference data.\n"
                "- `insight.json`: the reference run's final device-physics insight.\n"
                "- `STEP_LOG.md`: the reference run's accepted steps.\n"
                "- `meas/`: the reference device's calibration curves (forward branch). Context only, not calibration data.\n")
    (ws / "inputs" / "objective.md").write_text(objective_text(objective, program))
    # KB (manual only)
    shutil.copytree(HERE / "kb_cache" / "manual", ws / "kb" / "manual")
    (ws / "kb" / "literature" / "README.md").write_text("One note per source: see .claude/skills/literature-evidence.\n")
    (ws / "notes" / "journal.md").write_text(f"# Journal of run {run_id}\n\n")
    # tools
    for t in ("kb_search", "evaluate", "ofat", "dfols", "deck_patch", "state", "jobs",
              "verify", "diagnose", "vd_consistency", "extract", "identify"):
        (ws / "tools" / f"{t}.py").write_text(TOOL_WRAPPER.format(name=t, lib=HERE / "lib"))
    # state + simulator support files
    init_workspace_state(ws, cap, start={"deck_dir": ref["deck_dir"], "params": ref["params"], "values": ref["values"],
                                         "note": f"reference model: run {ref['run']} deck {ref['deck']}, card {ref['eval']}"}
                         if ref else None)
    S.init_store(ws / "runs")
    if C.V000_VERIFIED_SEED and not ref:
        seed = C.V000_VERIFIED_SEED if isinstance(C.V000_VERIFIED_SEED, list) else [C.V000_VERIFIED_SEED]
        write_json(ws.parent / "harness_state" / "envelope.json", {"v000": {"verified": [dict(c) for c in seed]}})
    if objective == "J_objective":
        apply_objective_text(ws)
        frozen_sha = check_frozen_ss_windows(program)
    full = alignment == "full_objective"
    if full:
        apply_full_objective_text(ws)
        reg = read_json(ws / "state" / "registry.json")
        reg["params"]["WF_gate"].update({"lo": None, "hi": None, "basis": C.FULL_OBJECTIVE_WF_BASIS,
                                         "bounds_proposal": "none (set by the gate alignment)"})
        write_json(ws / "state" / "registry.json", reg)
    declared = [n for n in (ref or {}).get("params", {}) if n not in fixed_vals and n != "WF_gate"]
    if fixed_vals:
        if recal:
            apply_recal_full_text(ws, program, fixed_vals, declared, float(ref["values"]["WF_gate"]))
        else:
            apply_fixed_text(ws, program)
        reg = read_json(ws / "state" / "registry.json")
        for n, v in fixed_vals.items():
            reg["params"][n]["fixed"] = v
        write_json(ws / "state" / "registry.json", reg)
    if agent_backend != "claude_cli":
        apply_fabric_text(ws)
    from codex_backend import write_agents_md
    write_agents_md(ws)
    # device wording: fill the [[NAME]] places of the instruction texts from the device definition (config.TEXT)
    for p in sorted(ws.rglob("*.md")):
        if not str(p.relative_to(ws)).startswith("kb/manual"):
            t = p.read_text()
            if "[[" in t:
                p.write_text(C.render_text(t))
    meta = {"run_id": run_id, "role": role, "created_utc": now_utc(), "sdevice_run_cap": cap,
            "harness_git_head": git_head(), "sentaurus": C.SENTAURUS_VERSION, "threads": C.THREADS,
            "cpu_slots": C.CPU_SLOTS, "delta_p": C.DELTA_P,
            "program": program, "objective": objective, "rules": {r: True for r in rules},
            "sdevice_timeout_s": 900 if objective in C.TERM_OBJECTIVES else {"main": None, "recal": 900}[program],
            "reference": ({k: ref[k] for k in ("run", "eval", "deck", "J", "values")} if ref else None),
            "fixed_params": fixed_vals,
            "recalibration": ({"kind": f"{C.TEXT['RECAL_KIND']} recalibration with a {C.TEXT['RECAL_KIND']} effective gate "
                                       "offset (full-objective gate alignment) under a shared-contact assumption",
                               "gate_offset_start_eV": float(ref["values"]["WF_gate"]),
                               "reference_run": ref["run"], "reference_card": ref["eval"], "reference_deck": ref["deck"],
                               "card_rule": "fixed selection rule (postrun.py select)",
                               "fixed_shared": fixed_vals, "declared_channel_parameters": declared,
                               "reference_narrative_given": False} if recal else None),
            "ss_windows_frozen_sha256": locals().get("frozen_sha"),
            "alignment_loss": "full_objective" if full else None,
            "preset_bounds": {},
            "priority": None,
            **({"agent_backend": agent_backend, "agent_harness": "codex", "agent_model": agent_model, "text_only_model": bool(text_only_model)}
               if agent_backend != "claude_cli" else {}),
            "hashes": {str(p.relative_to(ws)): sha256_file(p) for p in sorted(ws.rglob("*"))
                       if p.is_file() and not str(p.relative_to(ws)).startswith(("kb/manual", "runs/support"))}}
    write_json(ws / ".tcadcal_workspace.json", meta)
    write_json(root / "orchestrator" / "workspace_manifest.json", meta)
    return ws


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--cap", type=int, required=True, help="sdevice run budget")
    ap.add_argument("--role", default="main", choices=["preflight", "pilot", "main", "repeat"])
    ap.add_argument("--start-from", help="run id whose frozen final model is the start model (reference)")
    ap.add_argument("--rules", help="comma-separated run rules (default: by program)")
    ap.add_argument("--recalibrate", action="store_true",
                    help="recalibration of a second device from a reference model (the selected model of "
                         "--start-from): gate offset set by the full objective, the parameters in --fixed held at "
                         "their reference values, declared channel parameters only (run rule parameters_only)")
    ap.add_argument("--fixed", help="comma-separated parameters held at the reference card values "
                    "(run rule fixed_work_functions, e.g. WF_gate,WF_sd)")
    ap.add_argument("--objective", default="J_objective", choices=["J_objective"], help="objective J of the manuscript (14 terms)")
    ap.add_argument("--alignment", default="full_objective", choices=["full_objective"],
                    help="gate offset: after every simulation, one common offset that minimizes J over both drain biases")
    ap.add_argument("--agent-backend", default="fabric", choices=list(AGENT_BACKENDS),
                    help="fabric: NVIDIA NeMo Fabric with Codex (codex_orchestrate.py)")
    ap.add_argument("--agent-model", default="nemotron-3.5-super-vl", help="model id recorded in the workspace meta (fabric backend)")
    ap.add_argument("--text-only-model", action="store_true", help="the agent's model reads text only (image reads refused)")
    a = ap.parse_args()
    rules = [r for r in a.rules.split(",") if r] if a.rules is not None else None
    fixed = [f for f in a.fixed.split(",") if f] if a.fixed else None
    root = C.RUNS_ROOT / a.run_id
    new_root = not root.exists()
    try:
        ws = build(a.run_id, a.cap, a.role, "recal" if a.recalibrate else "main", a.start_from, rules, fixed, a.objective,
                   a.alignment, a.recalibrate,
                   agent_backend=a.agent_backend, agent_model=a.agent_model, text_only_model=a.text_only_model)
    except BaseException:
        if new_root:   # a failed build leaves no partial run folder, so the run ID can be used again
            shutil.rmtree(root, ignore_errors=True)
        raise
    print(ws)


if __name__ == "__main__":
    main()
