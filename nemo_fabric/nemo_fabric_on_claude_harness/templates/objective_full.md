## Gate offset: one common offset set by the full objective
- **What WF_gate is.** The physical gate is [[GATE_METAL]] (`inputs/DEVICE_INFO.md`). WF_gate is the TCAD gate work function used as
  one effective electrostatic gate offset. It enters only through the flat-band voltage, and it may absorb electrostatic
  offsets that the model does not resolve. It is not a measured or extracted [[GATE_METAL]] work function.
- **Assumption.** One static effective gate offset, common to both V_D and to every later evaluation of
  the selected card.
- **The alignment.** After each simulation, the tools set WF_gate to the value that minimizes J (all its terms, both
  drain biases) over the gate offset. Changing WF_gate by d moves the whole transfer curve by d along V_G. The same gate
  setting is used at V_D = [[VD_LO]] V and [[VD_HI]] V: neither curve is aligned on its own. The alignment is recomputed in every
  evaluation.
- **What it sets and what it leaves.** The common offset removes the part of the error that one shift along V_G can
  remove. Every term keeps the part that the shift cannot remove.
- **No interval.** WF_gate has no bounds, and you do not propose any. The gate offset is the only rigid electrostatic
  offset of the model: a new parameter whose only effect is a uniform charge at the gate dielectric duplicates it and is
  not added.
- **Computational limits** (not physical bounds, and not evidence about the model):
  - The simulated sweep covers [[VG_SIM_START]] to [[VG_SIM_STOP]] V. A shift is applied within +-0.3 V of the simulated gate value, so no
    curve is extrapolated. J uses the measured [[VG_START]] to [[VG_STOP]] V points only.
  - When the minimum lies at the edge of that window, the tools re-simulate with the gate value moved to the edge and
    align again: at most 3 re-simulations (1.2 V in total) in one evaluation. Each re-simulation counts in the budget.
- **Outcomes** (`alignment` in the evaluation record):
  - `defined: true`: scored at the aligned gate offset.
  - `defined: false`: the simulated current at one of the calibration curves stays below the V_th level ([[VTH_LEVEL]]) in the
    simulated sweep, so no alignment is made. The evaluation is scored at the gate value it was simulated with. Such a
    card is never selected as final.
  - `at_window_edge: true`: the minimum still lies at the window edge after the re-simulations. The record says so.
- **Records.** Every aligned evaluation also reports its terms and metrics at the simulated gate value, before the
  shift.

