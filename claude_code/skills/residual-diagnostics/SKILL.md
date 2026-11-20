---
name: residual-diagnostics
description: How to read the device metrics and region residuals physically, decide which device quantity is deficient, and check with probe runs whether a deck parameter controls it before any search for new physics.
---

# Residual diagnostics: physics first

## 1. State the mismatch with numbers
Start from the J terms outside tolerance (`J_terms.outside_tolerance` in every evaluation). Then,
for each drain bias ([[VD_LO]] V and [[VD_HI]] V), write:
- dV_th (mV), dSS (%), dlog10 I_on (dec), E_lin (TCAD vs measured), and which of them are outside
  tolerance (|error| > 1 in tolerance units).
- The region residuals (off, subthreshold, turn_on, on): mean signed log error, mean r^2, and the
  same-sign fraction. A region with mean r^2 > 1 and >= 80 % same sign is a systematic mismatch.
- How the mismatch changes between V_D = [[VD_LO]] V and [[VD_HI]] V, including the V_th shift between the two
  V_D (TCAD and measured, mV).

## 2. Link the region to the device quantity that governs it
These are textbook transistor relations. They tell you what kind of quantity is deficient. They do
not tell you which model or which values apply to this material. That comes from the literature.

| Mismatch | Governing device quantity |
|---|---|
| V_th differs, by the same amount at both V_D, while SS and the on state agree | Flat-band voltage. Handled automatically by the gate work-function alignment while the alignment is inside its interval. When it stops at an interval bound, the common offset stays in the V_th terms |
| SS differs | How much channel charge responds to a change of the Fermi-level position, relative to the gate capacitance (subthreshold capacitance ratio) |
| Turn-on curvature differs (current rises too fast or too slowly after threshold) | How the free-carrier density and the effective mobility grow as the Fermi level approaches the conduction band |
| On-state current at V_D = [[VD_LO]] V differs with V_th correct | Channel mobility and source/drain series resistance |
| The mismatch differs between V_D = [[VD_LO]] V and [[VD_HI]] V | Source/drain contact barrier, drain-field effects in the channel and overlap regions |
| TCAD off current above the measured detection limit | Free-carrier density of the channel and leakage paths |

Then look inside the simulated device (skill `device-internals`, `python3 tools/diagnose.py --show E#####`): where the
charge sits (free or trapped, which location), where E_F sits relative to E_C, how much of V_D drops at the contacts. These
are predictions of the current model; they locate the deficient quantity in the model, they do not prove a mechanism.
For a mismatch between V_D = [[VD_LO]] V and [[VD_HI]] V, run the long-channel test (`python3 tools/vd_consistency.py --eval E#####`):
it tells you whether the measured drain dependence can come from the channel at all.

## 3. Check whether a deck parameter controls that quantity
- Run probe runs (`ofat.py --params ...`) on the deck parameters. The `effect` entry of each probe
  shows how the parameter changes V_th, SS, I_on, E_lin and each region residual.
- A parameter controls the deficient quantity only if its probe moves the targeted metric or
  region in the needed direction by a useful amount.
- Every probe result also gives `activation_and_sensitivity` per parameter: `ACTIVE` (Sentaurus applied it, from the log
  echo or a field read-back), `INACTIVE` (not applied: the deck does not implement it), `ACTIVATION_UNVERIFIED`, and
  `LOW_SENSITIVITY` (applied, but its probe step moves every J term by less than 0.1 tolerance at this card: it does not
  control the targeted residual here, though it may elsewhere). `identifiability` lists collinear parameter pairs: such a
  pair cannot be separated by these two curves, so fit and report it as a combination.
- If one or more parameters do, run DFO-LS on them jointly, with the targeted residual named in
  the step record.
- If none does, or DFO-LS stalls with the residual still outside tolerance among `VALID` evaluations, the deck may lack
  the physics for that quantity. Read the fit's `stall_report` (trade-offs, sensitivity) and choose from its `options`.
  A stall with numerical failures near the best point (`stall_numerically_limited`) is not such evidence (skill
  `numerical-recovery`). One search does not show that a whole model family cannot fix a residual: state what was tested.
- Probe runs report each parameter's effect twice: at a fixed gate work function and after the re-alignment. A parameter
  that moves the curves only at a fixed gate work function is degenerate with the alignment. You may deprioritize it
  for now, but do not fix it or call it physically unimportant.

## 4. Write the deficiency before searching
The deficiency statement (also required in a `model_patch` proposal) has five parts:
- **targeted:** the metric or region items, e.g. `SS@[[VD_LO_G]]`, `SS@[[VD_HI_G]]`, `region:subthreshold@[[VD_LO_G]]`
- **measured vs TCAD:** the numbers
- **physical reading:** the deficient device quantity, in one or two sentences
- **why existing parameters cannot fix it:** with the probe and DFO-LS job ids
- **internal observation:** one inside-the-device observation on the accepted model (eval id, what it shows, labelled as
  a model prediction)
- **search target:** the physical mechanism to look for in the literature for amorphous oxide
  semiconductor transistors, and the Sentaurus model class to look for in the manual

Search only for that deficiency. A candidate model is acceptable only if its documented effect acts
on the deficient quantity, it is a built-in Sentaurus model, and the literature gives its form and
parameter range for this material class.

Rank the candidates. List every model class whose documented effect could act on the deficient
quantity (at least two). For each, predict the sign and rough size of its effect on the targeted
residual and its side effects in other bias regions (for example, a model that adds charge also
moves V_th, and a transport model acts mainly above threshold). Choose the one whose predicted
effect matches the residual signature best, and state why the others were rejected. The ranking
goes into the `candidates` field of the `model_patch` proposal.

## 5. Do not compensate
Do not move a parameter to absorb a residual that its physics cannot produce. If an update lowers J
by trading one region against another while the targeted residual stays, reject it and record why.
