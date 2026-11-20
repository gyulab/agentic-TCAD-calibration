---
name: deck-patching
description: Permitted Sentaurus deck modifications, syntax rules, parameter tokens, and the guard checks applied to every model_patch proposal and rendered deck.
---

# Deck patching

## What is fixed (the guard rejects any change)
Everything not listed here may change, with a manual chunk that shows the syntax and the evidence
that the proposal requires.
- **Structure and dimensions.** The structure deck `sde_dvs.cmd` (geometry, thicknesses, materials,
  contacts, mesh) and `AreaFactor = [[AREA_FACTOR]]` (the [[W_UM]] um channel width).
- **The internal-quantity outputs.** The harness appends its own `CurrentPlot` section; a deck may not define one.
- **Permittivities.** The `Epsilon` and `Epsilon_aniso` entries of the [[GATE_DIELECTRIC]] block ([[K_GATE_DIELECTRIC]]). No epsilon
  setting anywhere else. The [[CHANNEL]] permittivity comes from `[[CHANNEL_PAR]]`, so the [[CHANNEL]] block keeps
  `insert = "./[[CHANNEL_PAR]]"`.
- **Measurement conditions.**
  - The `File` section.
  - The three electrodes `gate`, `source` and `drain` stay in the Electrode section, each with the
    start bias `Voltage=0` and no current or charge boundary condition. The bias is applied by the
    `Solve` section.
  - The bias sweep of the `Solve` section: every solve step, goal, plot and save.
  - The temperature: `Temperature = [[TEMPERATURE]]` in the global Physics block, and [[TEMPERATURE]] K wherever a
    temperature is set.
- **Solver settings.** The `Math` entries of the start deck, unchanged and in order, and the solve
  steps of the `Solve` section. Two additions are possible, only where the manual requires them for
  a Physics model in the deck:
  - a `Math` entry that declares something tied to a location of this structure (the entry names
    `Electrode=`, `Region=`, `Material=`, `RegionInterface=` or `MaterialInterface=`)
  - an equation added to the equation lists of the `Coupled` statements in `Solve`
- **The gate electrode entry.** Its work function is set by the alignment. The source and drain
  entries are not fixed beyond their start bias: everything else in them may change.
- **No user-defined models.** No PMI or user libraries, no include directives, no loading of
  external files, no ferroelectric polarization. The deck stays a single-device simulation (no
  circuit or mixed-mode sections).
- **Model families.** Every added family needs an activation check, stated in the proposal field `activation_check`.
- **Parameter file.** One `Material` block each for `[[CHANNEL]]` and `[[GATE_DIELECTRIC]]` (the materials of this
  structure). Other blocks may only name a location (`Region=`, `RegionInterface=`,
  `MaterialInterface=`, `Electrode=`).

## Edits
Each edit is an exact string replacement: `"old"` must occur exactly once in the current
version of the file, and is replaced by `"new"`. Copy `"old"` from `deck_patch.py --show vNNN`.
With `"all": true`, every occurrence of `"old"` is replaced (for a line repeated in several
statements).

## Parameters
- A new numeric parameter is written as a token `@NAME@` in the deck text and registered in
  `new_params` with `init`, `lo`, `hi`, `scale` (`log` for densities and other quantities that
  span decades, `linear` otherwise), `unit`, `meaning` and `basis`.
- Values that are not calibrated may be written as literal numbers, with their basis in the
  proposal.
- Electrode names: `gate`, `source`, `drain`.
- Region and interface names in this structure: [[REGION_NAMES]]

## Syntax
- Follow the syntax shown in the manual chunk you cite. Check keyword spelling and units
  (the manual states the unit of each parameter).
- After `deck_patch.py --proposal P###`, a smoke-test evaluation runs at the base values. A deck
  error stops the run before any solve (no recovery levels are spent). Read the smoke result: if
  it failed, write a corrected proposal.

## Activation of a patch
The smoke test reports, for every parameter of the new deck version, whether Sentaurus applied it (`activation`, from the
log echo of the run and from field read-back): `ACTIVE`, `INACTIVE` (the requested model or value is not applied at the
intended location: the deck does not implement the approved model; correct the patch with a corrected proposal) or
`ACTIVATION_UNVERIFIED`. An evaluation with an `INACTIVE` parameter cannot be accepted. The smoke test also lists any
switched-on model whose [[CHANNEL]] parameters would come from the
[[PAR_TEMPLATE]] template of the material file (`silicon_template_models`); state the [[CHANNEL]] values and their source in the patch.

## After a successful patch
Probe runs on the new parameters (and on existing ones that act on the same device quantity), then
DFO-LS on them jointly. The new model is accepted only when an evaluation on the new deck version
lowers J by at least 1e-3, the targeted residual of the deficiency shrinks, and the fitted values
stay inside the ranges stated in the approved proposal.
