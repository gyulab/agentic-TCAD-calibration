# Device and measurement (fixed facts)

## Device
| Item | Value |
|---|---|
| Device | Fabricated bottom-gate transistor with an amorphous In-W-O (IWO) channel, **4 % W** |
| Channel length L (source/drain inner-edge gap) | 1.0 um |
| Channel width W | 5.0 um (2D simulation, AreaFactor = 5, current in A for the full width) |
| Source/drain overlap with the gate | 1.5 um per side (3.0 um total) |
| IWO thickness | 3.5 nm |
| Gate electrode | Pd, bottom gate |
| Gate dielectric | HfO2, 7.0 nm, bottom gate |
| Passivation | Conformal HfO2, 5.0 nm |
| Source/drain contacts | Pd, 40 nm, on top of the channel |
| Dielectric constants | k_IWO = 9, k_HfO2 = 17 (fixed inputs) |

The structure, the gate stack, the passivation and the contacts are the same as those of the
reference device (2 % W, same dimensions). Only the W content of the
channel differs. The 2D structure in `state/decks/v000/sde_dvs.cmd` implements these values. The
harness reads every mesh back and stops if any dimension differs.

## Measurement
- Semiconductor parameter analyzer, three-terminal I_D-V_G at fixed V_D, source grounded,
  room temperature.
- Gate sweep from -0.7 V to 2.0 V in 50 mV steps. Only the forward branch (-0.7 V to 2.0 V,
  55 points) is provided and used.
- Calibration curves: V_D = 0.05 V and V_D = 1.0 V (`inputs/meas/`). Columns: `VG_V`, `ID_A`
  (signed drain current as measured, values near zero in the off state are instrument noise).

## Measurement protocol facts (supplied measurement facts)
- One die. Each drain bias was swept once. Only the two calibration curves are provided.
- No capacitance-voltage (C-V) measurement exists for this device.
- One channel length is available for this calibration. There is no channel-length series of the same die.

## Start model (deck v000)
- The model calibrated on the reference device (2 % W, same structure) in an earlier calibration: its deck and the
  parameter values of its selected card (`inputs/reference/`). This run's deck v000 is that deck.
- The source/drain contact parameters of that card are fixed in this run. The gate offset starts at that card's
  value and is set by the gate alignment, one common offset for both drain biases at the minimum of J
  (`inputs/objective.md`).
