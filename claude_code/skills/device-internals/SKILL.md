---
name: device-internals
description: How to read the inside of the simulated device (charge budget, band edges, quasi-Fermi potentials, Fermi-level efficiency, gate capacitance, band diagrams), the long-channel drain-bias test and the extraction tool, and what these model predictions can and cannot show.
---

# Inside the device

Terminal curves often cannot separate competing explanations. The harness therefore records, for every simulation, what
happens inside the simulated device. **These are predictions of the model you are testing, not measurements.** Use them to
locate where charge and voltage go in the model, to check that a parameter acts where you think it acts, and to design a
discriminating check. They are never evidence that a mechanism is right: that still needs the measured residual, the
manual and the literature.

## In every evaluation (no call needed)
Every evaluation view, probe result (`base_inside_the_device`, and `inside_change` per probe) and fit result carries
`inside_the_device`, computed from the stored snapshots and the per-bias table. Per V_D and per bias point (off,
10 nA, V_th, on), from the snapshot nearest that V_G:
- `Ec_max_near_source_eV`: the conduction-band maximum near the inner source edge, relative to the source Fermi level.
- `Ec_minus_EFn_centre_eV`: E_C - E_Fn at the channel centre (mid film).
- `V_D_share` (at and above V_th - 0.2 V, from the per-bias table at the exact V_G) or `V_D_partition` (otherwise, from
  the snapshot): the drop of the electron quasi-Fermi level at the source access, along the channel and at the drain
  access, as fractions of V_D.
- `film_Ec_front_minus_back_eV`: E_C at the gate interface minus E_C at the back interface (channel centre).
- `current_front_half_fraction`: the share of the electron current in the gate-side half of the film (channel centre).
- `trapped_charge`: trapped electrons per area at the interface (and in the bulk, when the deck has bulk states)
  10 nm inside the source edge, at the centre and 10 nm inside the drain edge.
- the per-bias table values: sheet densities, `eta_F`, `C_GG_over_C_ox`, `V_D_share`.
- `contact_edge_artifact`: the largest conduction-band spike above both neighbours at an inner contact edge over the
  sweep (artifact screen). A flagged spike is also listed in `validity_notes`.
- `drain_response_at_fixed_V_G`: the same card, cuts and gate voltage (the snapshot nearest the [[VD_LO_G]] V threshold) at
  V_D = [[VD_LO_G]] and [[VD_HI_G]] V, with energies relative to the source Fermi level. It gives the E_C maximum near the source, E_C -
  E_Fn at the centre, and trapped charge at the source edge, centre and drain edge, with the change. Comparing each
  V_D at its own threshold mixes a V_G change into the V_D change, so use this block for drain-bias effects.
- `gate_alignment_estimates`: the aligned gate work function, chi of [[CHANNEL]], phi_G - chi and phi_SD - chi. Also the
  trapped and donor sheet charge at V_th (channel centre, V_D [[VD_LO_G]] V), the voltage each corresponds to across the gate
  oxide (q N / C_ox), and `phi_G_charge_compensated_estimate_eV` (the aligned value plus those voltages).
  - These are approximate estimates. The trapped charge depends on bias, a local value is not an exact 2D charge
    removal, and phi_G - chi is not V_FB.
  - None of them is an extracted work function.

The summary gives observables and their definitions, never an explanation: a co-occurrence (for example a trapped-
charge change and a V_th change) is not a cause. Interpreting is your task, with the alternative readings and the
limits. Use these in three roles, and say which one in `supports`:
1. **Locate** where the targeted quantity sits in the model (source contact, channel, drain edge, interface, film).
2. **Check** that the model state is physical (contact-edge artifacts, a voltage lost at a contact, saturated trap
   occupancy).
3. **Check plausibility** of fitted values (for example how much of the gate alignment the modelled charges explain).

## Tools (each prints JSON)
| Command | What it gives |
|---|---|
| `python3 tools/diagnose.py --show E#####` | The inside-the-device table at four bias points (off at V_G [[VG_START_G]] V, 10 nA, V_th at [[VTH_LEVEL]], on at V_G [[ION_VG_G]] V), both V_D |
| `python3 tools/diagnose.py --bands E##### --vd [[VD_HI_G]] [--vg -0.3,0.5]` | Band diagrams from the snapshots saved during the gate sweep (dense around threshold): vertical cuts through the film at the channel centre and 10 nm inside each contact edge, lateral cuts along the film; a PNG (`evals/E#####_bands_vd<V_D>.png`) and a compact table |
| `python3 tools/diagnose.py --compare E##### E#####` | What changed inside from one evaluation to another |
| `python3 tools/diagnose.py --observability E#####` | The window of E_C - E_Fn at the gate interface that the measured V_G range sweeps |
| `python3 tools/diagnose.py --run E##### --vd [[VD_HI_G]]` | Extra diagnostic run with snapshots about every 0.1 V (diagnostic runs: at most 40, inside the run budget) |
| `python3 tools/vd_consistency.py [--eval E#####]` | Long-channel drain-bias test on the measured curves (and on a TCAD evaluation) |
| `python3 tools/extract.py [--eval E#####]` | The same extraction on measured and TCAD curves: local SS(V_G), effective sub-gap state density from SS, linear field-effect mobility |
| `python3 tools/identify.py J####` | Identifiability and sensitivity from a finished probe job |

## The inside-the-device table
- **E_C - E_Fn (eV)** at probe points: channel centre (gate interface, mid film, back interface), 10 nm inside each inner
  contact edge, 10 nm under each contact. Small or negative values mean the conduction band is at or below the electron
  quasi-Fermi level (accumulation); values of several tenths of an eV mean the film is depleted at that point.
- **Sheet densities (cm^-2):** free electrons in the channel and under each contact; trapped electrons in the channel bulk
  and at each interface with traps. The ratio of trapped to free charge in subthreshold tells you which charge class
  absorbs the gate charge there.
- **V_D share (fractions of V_D):** where the drain bias drops along the electron quasi-Fermi potential: source access
  (from the source contact to 10 nm inside the channel), channel, drain access. Reported at and above V_th - 0.2 V only.
  A large source-access share means the current is limited before it enters the channel; band bending without a
  quasi-Fermi-level drop is not a resistive loss.
- **eta_F = d(E_Fn - E_C)/d(qV_G)** at the channel centre: how efficiently the gate moves the Fermi level. Low values mean
  the gate charge goes into states (traps, depletion of donors) instead of moving E_F.
- **C_GG/C_ox:** quasi-static gate capacitance from the gate charge, relative to the oxide capacitance of the gated area.

## Long-channel drain-bias test
With local transport and traps in equilibrium with the local quasi-Fermi level, the sheet conductance depends only on
V_G - V along the channel, so the [[VD_HI_G]] V curve follows from the [[VD_LO_G]] V curve (gradual-channel integral). The tool reports the
excess of the actual [[VD_HI_G]] V curve over this prediction, per operating region. If the measured curves show an excess that the
TCAD curves of your model do not, no change of the channel sub-gap states, the donor density or a density-dependent
mobility can produce it: it breaks one of the test's assumptions (the current is set by the channel conductance alone; the
local conductance depends on V_G - V only; the curves are steady states independent of the sweep history).

## Rules
- **Run rule `band_reading`** (fields in skill `calibration-loop`):
  - A parameter step cites the relevant summary values, or states that the interior diagnostics identify no
    additional mechanism.
  - A model addition reads one band diagram and the charge distribution of the accepted model, with an interpretation
    and its alternatives.
  - The final model gets one interior check: vertical and lateral band cuts at both V_D, the charge balance and the
    contact voltage drop, plus the gate alignment estimates.
- Internal quantities are never an `evidence` item. Evidence stays manual and literature quotes.
- The QC of the measured data (detection limit, gate leakage, hysteresis) is not part of your inputs; work with the
  forward curves as given.
